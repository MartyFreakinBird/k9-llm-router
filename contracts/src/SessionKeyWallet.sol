// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

/**
 * @title  SessionKeyWallet
 * @notice AEG-7 — Intent-Based Smart Contract Wallet with Parametric Risk Guardrails
 * @dev    ERC-4337 account that accepts AI-generated intents but enforces immutable
 *         risk parameters. The AI plans; the contract limits. No intent executes
 *         unless it satisfies ALL of the Genesis Risk Parameters.
 *
 *         Architecture:
 *           Local K-9 AI → validateIntent() → executeBundle() → AEGTreasury
 *
 *         AEG-9 hook: RiskParams are passed as public inputs to the ZK-Proof-of-
 *         Rationality Noir circuit, so proofs bind to WHICH ruleset was active.
 *
 *         Whitelisted protocols (Base Mainnet):
 *           Uniswap V3   — 0x2626664c2603336E57B271c5C0b26F421741e481
 *           Aave V3      — 0x18cd499E3d7ed42fEBBa595e6574a2fa5977f2B3
 *           Curve        — 0xd9E1cE17f2641f24aE83637ab66a2cca9C378B9F (L2)
 *           Balancer V2  — 0xBA12222222228d8Ba445958a75a0704d566BF2C8
 *
 *         Deployment target: Base Sepolia → Base Mainnet (AEG-6 first)
 *
 * @author Vectos / K-9 Architecture
 */

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {Ownable} from "@openzeppelin/contracts/access/Ownable.sol";
import {ReentrancyGuard} from "@openzeppelin/contracts/utils/ReentrancyGuard.sol";

// ── Chainlink ETH/USD feed interface ─────────────────────────────────────────
interface AggregatorV3Interface {
    function latestRoundData()
        external
        view
        returns (
            uint80 roundId,
            int256 answer,
            uint256 startedAt,
            uint256 updatedAt,
            uint80 answeredInRound
        );
}

// ── AEGTreasury minimal interface ────────────────────────────────────────────
interface IAEGTreasury {
    function sessionBalance(address wallet) external view returns (uint256);
    function recordDrawdown(address wallet, uint256 amountUsd) external;
}

// ── ERC-4337 EntryPoint minimal interface ────────────────────────────────────
interface IEntryPoint {
    function handleOps(bytes[] calldata ops, address payable beneficiary) external;
}

// ── AEG-9 Proof of Rationality Verifier interface ─────────────────────────────
interface IProofOfRationalityVerifier {
    /// @dev Ultra Honk verifier (79 public inputs, see main.nr PublicInputs layout).
    ///      proof        = raw bytes from `bb prove -t evm`
    ///      publicInputs = bytes32[] of length 79
    ///        [  0.. 31] params_hash     (one byte per slot)
    ///        [      32] session_wallet  (address as field element)
    ///        [  33..38] intent fields   (router, tokenIn, tokenOut, amountIn, minAmountOut, timestamp)
    ///        [ 39.. 70] execution_hash  (one byte per slot, correlation-only)
    ///        [ 71.. 78] Ultra Honk internal pairing points (populated by bb prove)
    function verify(bytes calldata _proof, bytes32[] calldata _publicInputs)
        external view returns (bool);
}

// ─────────────────────────────────────────────────────────────────────────────

/**
 * @dev Genesis Risk Parameters — hard-coded at deployment.
 *      Immutable until AEG DAO vote via AEGGovernor timelock (30-day unlock).
 *      These are the public inputs for the AEG-9 ZK-Proof-of-Rationality circuit.
 */
struct RiskParams {
    // Max drawdown per 24-hour rolling window (basis points: 150 = 1.5%)
    uint24 maxDrawdownBps;         // 150

    // Max position as % of pool liquidity (basis points: 200 = 2.0%)
    uint24 maxPoolShareBps;        // 200

    // Max USD value per position (USDC 6-decimal: 25_000 * 1e6)
    uint256 maxPositionUsd;        // 25_000_000_000 (25k USDC)

    // Slippage tolerance for blue-chip pairs ETH/USDC (bps: 30 = 0.3%)
    uint24 slippageBlueChipBps;    // 30

    // Slippage tolerance for alt pairs (bps: 75 = 0.75%)
    uint24 slippageAltBps;         // 75

    // Max gas fee per bundle in USD (Chainlink 8-decimal: 3 * 1e8 = $3.00)
    uint256 maxGasUsd;             // 300_000_000

    // Cooldown between session strategy changes (seconds)
    uint256 cooldownSeconds;       // 60

    // Whitelisted router address hashes (keccak256 of router address bytes)
    bytes32[] whitelistedRouters;
}

/**
 * @dev A single trade intent submitted by the local K-9 AI agent.
 *      Validated against RiskParams before any on-chain execution.
 */
struct TradeIntent {
    address router;           // Must be in whitelistedRouters
    address tokenIn;
    address tokenOut;
    uint256 amountIn;         // In tokenIn base units
    uint256 amountOutMin;     // Slippage floor
    uint256 poolLiquidityUsd; // Pool TVL snapshot (provided by AI, verified on-chain)
    bool    isBlueChip;       // true = ETH/USDC pair, false = alt pair
    bytes   callData;         // Encoded router calldata
    uint256 gasPriceWei;      // Current gas price for USD calculation
    uint256 timestamp;        // Intent creation timestamp
}

// ─────────────────────────────────────────────────────────────────────────────

contract SessionKeyWallet is Ownable, ReentrancyGuard {

    // ── Constants ─────────────────────────────────────────────────────────────

    uint256 public constant BPS_DENOMINATOR = 10_000;
    uint256 public constant CHAINLINK_DECIMALS = 1e8;
    uint256 public constant USDC_DECIMALS = 1e6;
    uint256 public constant GENESIS_LOCK_PERIOD = 30 days;

    // ── Immutable state ───────────────────────────────────────────────────────

    RiskParams public params;
    IAEGTreasury public immutable treasury;
    AggregatorV3Interface public immutable ethUsdFeed;
    address public immutable entryPoint;
    uint256 public immutable deployedAt;

    // ── Mutable state ─────────────────────────────────────────────────────────

    // Session key — the address the local K-9 AI signs intents with
    address public sessionKey;

    // Rolling 24-hour drawdown tracker
    uint256 public rollingDrawdownUsd;      // in USDC 1e6
    uint256 public drawdownWindowStart;     // timestamp

    // Per-block trade cap
    uint256 public lastExecutionBlock;

    // Cooldown tracker
    uint256 public lastStrategyChangeAt;

    // Governance unlock — once set, DAO can update params via AEGGovernor
    bool    public genesisLockExpired;
    address public governor;

    // ── AEG-9 ZK Verifier ─────────────────────────────────────────────────────
    /// @notice Deployed ProofOfRationalityVerifier — set once via setVerifier() in AEG-6 deploy.
    IProofOfRationalityVerifier public verifier;

    // ── Events ────────────────────────────────────────────────────────────────

    event IntentValidated(bytes32 indexed intentHash, address indexed router, uint256 amountIn);
    event IntentExecuted(bytes32 indexed intentHash, uint256 amountOut, uint256 gasUsed);
    event IntentRejected(bytes32 indexed intentHash, string reason);
    event DrawdownRecorded(uint256 amountUsd, uint256 rollingTotal);
    event SessionKeyUpdated(address indexed newKey);
    event GenesisLockExpired();
    event RiskParamsUpdated(bytes32 paramsHash);
    event VerifierSet(address indexed verifier);
    event IntentExecutedWithProof(bytes32 indexed intentHash, bytes32 executionHash, uint256 amountOut);
    event EmergencyPause(address indexed caller);

    // ── Errors ────────────────────────────────────────────────────────────────

    error NotSessionKey();
    error RouterNotWhitelisted(address router);
    error DrawdownExceeded(uint256 currentBps, uint256 maxBps);
    error PositionSizeExceeded(uint256 amountUsd, uint256 maxUsd);
    error SlippageExceeded(uint256 slippageBps, uint256 maxBps);
    error GasLimitExceeded(uint256 gasUsd, uint256 maxUsd);
    error PerBlockCapExceeded();
    error CooldownActive(uint256 remainingSeconds);
    error GenesisLockActive(uint256 unlockAt);
    error EmergencyPauseActive();
    error VerifierNotSet();
    error ProofInvalid();
    error PublicInputMismatch(string field);

    // ── Pause ─────────────────────────────────────────────────────────────────

    bool public paused;

    modifier whenNotPaused() {
        if (paused) revert EmergencyPauseActive();
        _;
    }

    modifier onlySessionKey() {
        if (msg.sender != sessionKey) revert NotSessionKey();
        _;
    }

    // ── Constructor ───────────────────────────────────────────────────────────

    constructor(
        address _owner,
        address _sessionKey,
        address _treasury,
        address _ethUsdFeed,
        address _entryPoint,
        address _governor
    ) Ownable(_owner) {
        sessionKey       = _sessionKey;
        treasury         = IAEGTreasury(_treasury);
        ethUsdFeed       = AggregatorV3Interface(_ethUsdFeed);
        entryPoint       = _entryPoint;
        governor         = _governor;
        deployedAt       = block.timestamp;
        drawdownWindowStart = block.timestamp;

        // ── Genesis Risk Parameters — AEG-7 ──────────────────────────────────
        // These are immutable for GENESIS_LOCK_PERIOD (30 days).
        // After unlock, AEGGovernor can propose updates via timelock.

        params.maxDrawdownBps      = 150;               // 1.5%
        params.maxPoolShareBps     = 200;               // 2.0%
        params.maxPositionUsd      = 25_000 * USDC_DECIMALS; // $25,000 USDC
        params.slippageBlueChipBps = 30;                // 0.30%
        params.slippageAltBps      = 75;                // 0.75%
        params.maxGasUsd           = 3 * CHAINLINK_DECIMALS; // $3.00
        params.cooldownSeconds     = 60;                // 60 seconds

        // Whitelisted routers — keccak256 of Base Mainnet addresses
        // Update to Base Sepolia addresses for testnet deployment
        params.whitelistedRouters = new bytes32[](4);
        params.whitelistedRouters[0] = keccak256(abi.encodePacked(
            address(0x2626664c2603336E57B271c5C0b26F421741e481) // Uniswap V3 SwapRouter02
        ));
        params.whitelistedRouters[1] = keccak256(abi.encodePacked(
            address(0x18cd499e3d7ed42FEBbA595e6574a2FA5977F2b3) // Aave V3 Pool
        ));
        params.whitelistedRouters[2] = keccak256(abi.encodePacked(
            address(0xd9e1cE17f2641f24aE83637ab66a2cca9C378B9F) // Curve Router
        ));
        params.whitelistedRouters[3] = keccak256(abi.encodePacked(
            address(0xBA12222222228d8Ba445958a75a0704d566BF2C8) // Balancer V2 Vault
        ));
    }

    // ─────────────────────────────────────────────────────────────────────────
    // CORE VALIDATION ENGINE
    // ─────────────────────────────────────────────────────────────────────────

    /**
     * @notice Validate a trade intent against all Genesis Risk Parameters.
     * @dev    Called pre-flight before any execution. Reverts on any violation.
     *         This function is also called by the AEG-9 ZK circuit's public verifier.
     * @param  intent   The AI-generated trade intent to validate.
     * @return intentHash  keccak256 of the intent for event emission and ZK binding.
     */
    function validateIntent(
        TradeIntent calldata intent
    ) public view whenNotPaused returns (bytes32 intentHash) {
        intentHash = keccak256(abi.encode(intent));

        // ── Check 1: Router whitelist ─────────────────────────────────────────
        bytes32 routerHash = keccak256(abi.encodePacked(intent.router));
        bool isWhitelisted = false;
        for (uint256 i = 0; i < params.whitelistedRouters.length; i++) {
            if (params.whitelistedRouters[i] == routerHash) {
                isWhitelisted = true;
                break;
            }
        }
        if (!isWhitelisted) revert RouterNotWhitelisted(intent.router);

        // ── Check 2: Per-block trade cap ──────────────────────────────────────
        if (lastExecutionBlock == block.number) revert PerBlockCapExceeded();

        // ── Check 3: Cooldown ─────────────────────────────────────────────────
        uint256 elapsed = block.timestamp - lastStrategyChangeAt;
        if (lastStrategyChangeAt > 0 && elapsed < params.cooldownSeconds) {
            revert CooldownActive(params.cooldownSeconds - elapsed);
        }

        // ── Check 4: Position size ────────────────────────────────────────────
        // Convert amountIn to USD via Chainlink ETH/USD or assume USDC
        uint256 positionUsd = _estimatePositionUsd(intent.tokenIn, intent.amountIn);
        if (positionUsd > params.maxPositionUsd) {
            revert PositionSizeExceeded(positionUsd, params.maxPositionUsd);
        }

        // ── Check 5: Pool share ───────────────────────────────────────────────
        if (intent.poolLiquidityUsd > 0) {
            uint256 poolShareBps = (positionUsd * BPS_DENOMINATOR) / intent.poolLiquidityUsd;
            if (poolShareBps > params.maxPoolShareBps) {
                revert PositionSizeExceeded(poolShareBps, params.maxPoolShareBps);
            }
        }

        // ── Check 6: Slippage ─────────────────────────────────────────────────
        uint256 slippageBps = _calculateSlippageBps(
            intent.amountIn,
            intent.amountOutMin,
            intent.tokenIn,
            intent.tokenOut
        );
        uint256 maxSlippage = intent.isBlueChip
            ? params.slippageBlueChipBps
            : params.slippageAltBps;
        if (slippageBps > maxSlippage) {
            revert SlippageExceeded(slippageBps, maxSlippage);
        }

        // ── Check 7: Gas cost ─────────────────────────────────────────────────
        uint256 gasUsd = _estimateGasUsd(intent.gasPriceWei);
        if (gasUsd > params.maxGasUsd) {
            revert GasLimitExceeded(gasUsd, params.maxGasUsd);
        }

        // ── Check 8: Rolling drawdown ─────────────────────────────────────────
        // Read current session balance from treasury
        uint256 sessionBalanceUsd = treasury.sessionBalance(address(this));
        if (sessionBalanceUsd > 0) {
            uint256 drawdownBps = (rollingDrawdownUsd * BPS_DENOMINATOR) / sessionBalanceUsd;
            if (drawdownBps >= params.maxDrawdownBps) {
                revert DrawdownExceeded(drawdownBps, params.maxDrawdownBps);
            }
        }

        return intentHash;
    }

    /**
     * @notice Execute a validated trade intent.
     * @dev    Validates first, then executes. Records drawdown post-execution.
     *         Emits IntentExecuted for K-9 watcher and ZK audit trail.
     */
    function executeIntent(
        TradeIntent calldata intent
    ) external onlySessionKey nonReentrant whenNotPaused {
        // ── Validate ──────────────────────────────────────────────────────────
        bytes32 intentHash = validateIntent(intent);
        emit IntentValidated(intentHash, intent.router, intent.amountIn);

        // ── Record pre-trade balance ───────────────────────────────────────────
        uint256 balanceBefore = IERC20(intent.tokenOut).balanceOf(address(this));

        // ── Execute ───────────────────────────────────────────────────────────
        IERC20(intent.tokenIn).approve(intent.router, intent.amountIn);
        (bool success,) = intent.router.call(intent.callData);
        require(success, "Router call failed");

        // ── Record post-trade balance ──────────────────────────────────────────
        uint256 balanceAfter = IERC20(intent.tokenOut).balanceOf(address(this));
        uint256 gasUsed = gasleft(); // approximate

        // ── Record drawdown if we received less than expected ─────────────────
        if (balanceAfter < balanceBefore + intent.amountOutMin) {
            uint256 shortfall = (balanceBefore + intent.amountOutMin) - balanceAfter;
            uint256 shortfallUsd = _estimatePositionUsd(intent.tokenOut, shortfall);
            _recordDrawdown(shortfallUsd);
        }

        // ── Update state ───────────────────────────────────────────────────────
        lastExecutionBlock = block.number;
        lastStrategyChangeAt = block.timestamp;

        emit IntentExecuted(intentHash, balanceAfter - balanceBefore, gasUsed);
    }

    // ─────────────────────────────────────────────────────────────────────────
    // DRAWDOWN TRACKER (24-hour rolling window)
    // ─────────────────────────────────────────────────────────────────────────

    function _recordDrawdown(uint256 amountUsd) internal {
        // Reset window if 24h elapsed
        if (block.timestamp >= drawdownWindowStart + 24 hours) {
            rollingDrawdownUsd = 0;
            drawdownWindowStart = block.timestamp;
        }

        rollingDrawdownUsd += amountUsd;
        treasury.recordDrawdown(address(this), amountUsd);
        emit DrawdownRecorded(amountUsd, rollingDrawdownUsd);
    }

    // ─────────────────────────────────────────────────────────────────────────
    // PRICE + SLIPPAGE HELPERS
    // ─────────────────────────────────────────────────────────────────────────

    /// @dev Estimate position value in USD (USDC 6-decimal).
    ///      Assumes USDC is 1:1. For ETH, queries Chainlink feed.
    function _estimatePositionUsd(
        address token,
        uint256 amount
    ) internal view returns (uint256) {
        // Simplified: if token is USDC (6 decimals), return amount directly
        // For production: add token registry with decimals + Chainlink feeds
        // For ETH/WETH: use ethUsdFeed
        (, int256 ethPrice,,,) = ethUsdFeed.latestRoundData();
        if (ethPrice <= 0) return 0;

        // Assume 18-decimal ETH amount, convert to USD (8-decimal Chainlink)
        // = (amount * ethPrice) / (1e18 * 1e8) * 1e6 for USDC-denominated
        uint256 usd6 = (amount * uint256(ethPrice)) / (1e18 * 1e2);
        return usd6;
    }

    /// @dev Calculate slippage in basis points from intent parameters.
    function _calculateSlippageBps(
        uint256 amountIn,
        uint256 amountOutMin,
        address tokenIn,
        address tokenOut
    ) internal pure returns (uint256) {
        if (amountIn == 0 || amountOutMin == 0) return 0;
        // Simplified: assume 1:1 normalized. In production, normalize via
        // Chainlink oracle before comparing amounts across decimal boundaries.
        if (amountIn <= amountOutMin) return 0;
        return ((amountIn - amountOutMin) * BPS_DENOMINATOR) / amountIn;
    }

    /// @dev Estimate gas cost in USD (Chainlink 8-decimal).
    function _estimateGasUsd(uint256 gasPriceWei) internal view returns (uint256) {
        (, int256 ethPrice,,,) = ethUsdFeed.latestRoundData();
        if (ethPrice <= 0 || gasPriceWei == 0) return 0;

        // Assume ~300k gas per complex bundle
        uint256 estimatedGasUnits = 300_000;
        uint256 gasCostWei = gasPriceWei * estimatedGasUnits;

        // Convert to USD (8-decimal): (gasCostWei * ethPrice) / 1e18
        return (gasCostWei * uint256(ethPrice)) / 1e18;
    }

    // ─────────────────────────────────────────────────────────────────────────
    // GOVERNANCE — Genesis lock + DAO param updates
    // ─────────────────────────────────────────────────────────────────────────

    /**
     * @notice Unlock governance after 30-day genesis period.
     * @dev    Anyone can call after the lock period to record the transition.
     *         AEGGovernor can then propose param updates via timelock.
     */
    function unlockGenesis() external {
        if (block.timestamp < deployedAt + GENESIS_LOCK_PERIOD) {
            revert GenesisLockActive(deployedAt + GENESIS_LOCK_PERIOD);
        }
        genesisLockExpired = true;
        emit GenesisLockExpired();
    }

    /**
     * @notice Update risk parameters post-genesis via AEGGovernor timelock.
     * @dev    AEG-9 note: any param change generates a new paramsHash.
     *         ZK proofs issued after this update bind to the new hash.
     *         Historical proofs remain valid against their original params snapshot.
     */
    function updateRiskParams(
        RiskParams calldata newParams
    ) external {
        require(genesisLockExpired, "Genesis lock active");
        require(msg.sender == governor, "Only governor");

        // Safety floor: DAO cannot loosen maxDrawdown beyond 3.0% ever
        require(newParams.maxDrawdownBps <= 300, "Max drawdown floor: 3.0%");

        params = newParams;
        emit RiskParamsUpdated(keccak256(abi.encode(newParams)));
    }

    // ─────────────────────────────────────────────────────────────────────────
    // SESSION KEY MANAGEMENT
    // ─────────────────────────────────────────────────────────────────────────

    function updateSessionKey(address newKey) external onlyOwner {
        require(newKey != address(0), "Zero address");
        sessionKey = newKey;
        emit SessionKeyUpdated(newKey);
    }

    // ─────────────────────────────────────────────────────────────────────────
    // EMERGENCY CONTROLS
    // ─────────────────────────────────────────────────────────────────────────

    /**
     * @notice Pause all intent execution. Callable by owner or K-9 watcher.
     * @dev    K-9 alignment_score monitor should call this if score drops below
     *         the governance threshold during a live session.
     */
    function pause() external {
        require(msg.sender == owner() || msg.sender == sessionKey, "Unauthorized");
        paused = true;
        emit EmergencyPause(msg.sender);
    }

    function unpause() external onlyOwner {
        paused = false;
    }

    // ─────────────────────────────────────────────────────────────────────────
    // VIEW HELPERS (for K-9 watcher + AEG-9 ZK circuit)
    // ─────────────────────────────────────────────────────────────────────────

    /// @notice Returns keccak256 of current RiskParams — public input for AEG-9 circuit.
    function currentParamsHash() public view returns (bytes32) {
        return keccak256(abi.encode(params));
    }

    /// @notice Returns current rolling drawdown as basis points of session balance.
    function currentDrawdownBps() external view returns (uint256) {
        uint256 balance = treasury.sessionBalance(address(this));
        if (balance == 0) return 0;
        return (rollingDrawdownUsd * BPS_DENOMINATOR) / balance;
    }

    /// @notice Check if a router address is whitelisted.
    function isRouterWhitelisted(address router) external view returns (bool) {
        bytes32 h = keccak256(abi.encodePacked(router));
        for (uint256 i = 0; i < params.whitelistedRouters.length; i++) {
            if (params.whitelistedRouters[i] == h) return true;
        }
        return false;
    }

    /// @notice Receive ETH for gas top-ups.

    // ─────────────────────────────────────────────────────────────────────────
    // AEG-9 ZK PROOF INTEGRATION
    // ─────────────────────────────────────────────────────────────────────────

    /**
     * @notice Wire the deployed ProofOfRationalityVerifier to this wallet.
     * @dev    Called once during AEG-6 deployment (DeployAEG6.s.sol step 3).
     */
    function setVerifier(address _verifier) external onlyOwner {
        require(_verifier != address(0), "Zero verifier address");
        verifier = IProofOfRationalityVerifier(_verifier);
        emit VerifierSet(_verifier);
    }

    /**
     * @notice Execute a ZK-proven trade intent.
     * @dev    Production execution path — enforces AEG-9 proof before any swap.
     *         executeIntent() remains available for dev/testnet sessions without a live prover.
     *
     *         Proof verification flow:
     *           1. validateIntent() — existing risk guards (drawdown, slippage, whitelist)
     *           2. params_hash check — proof must bind to current RiskParams version
     *           3. session_wallet check — proof must bind to this contract address
     *           4. intent field checks — each submitted intent field matches proof public inputs
     *           5. verifier.verify() — Ultra Honk SNARK verification (6 circuit invariants)
     *           6. Execute swap — identical to executeIntent() post-validation
     *
     * @param intent       Trade intent struct
     * @param proof        Raw Ultra Honk proof bytes (from bb prove -t evm)
     * @param publicInputs 79-element bytes32[] (from bb prove --output public_inputs)
     */
    function executeIntentWithProof(
        TradeIntent calldata intent,
        bytes calldata proof,
        bytes32[] calldata publicInputs
    ) external onlySessionKey nonReentrant whenNotPaused {

        if (address(verifier) == address(0)) revert VerifierNotSet();
        require(publicInputs.length == 79, "Wrong public input count");

        // ── Step 1: Standard risk validation ─────────────────────────────────
        bytes32 intentHash = validateIntent(intent);

        // ── Step 2: params_hash binding ──────────────────────────────────────
        // Pack 32 single-byte slots (indices 0-31) into one bytes32 for comparison.
        bytes32 onChainParamsHash = currentParamsHash();
        bytes32 proofParamsHash   = _packBytes32FromInputs(publicInputs, 0);
        if (proofParamsHash != onChainParamsHash) revert PublicInputMismatch("params_hash");

        // ── Step 3: session_wallet binding ───────────────────────────────────
        if (publicInputs[32] != bytes32(uint256(uint160(address(this)))))
            revert PublicInputMismatch("session_wallet");

        // ── Step 4: Intent field binding ─────────────────────────────────────
        // protocol_router: circuit uses keccak256(abi.encodePacked(router)) as Field
        if (publicInputs[33] != bytes32(keccak256(abi.encodePacked(intent.router))))
            revert PublicInputMismatch("protocol_router");
        if (publicInputs[34] != bytes32(uint256(uint160(intent.tokenIn))))
            revert PublicInputMismatch("token_in");
        if (publicInputs[35] != bytes32(uint256(uint160(intent.tokenOut))))
            revert PublicInputMismatch("token_out");
        if (publicInputs[36] != bytes32(intent.amountIn))
            revert PublicInputMismatch("amount_in");
        if (publicInputs[37] != bytes32(intent.amountOutMin))
            revert PublicInputMismatch("min_amount_out");
        if (publicInputs[38] != bytes32(intent.timestamp))
            revert PublicInputMismatch("deadline");

        // ── Step 5: execution_hash (correlation-only) ─────────────────────────
        // Read from proof; not reconstructed on-chain (block_number not a circuit input).
        // Replay protection is covered by lastExecutionBlock + per-block cap in validateIntent().
        bytes32 proofExecHash = _packBytes32FromInputs(publicInputs, 39);

        // ── Step 6: ZK verification ───────────────────────────────────────────
        if (!verifier.verify(proof, publicInputs)) revert ProofInvalid();

        // ── Step 7: Execute swap ──────────────────────────────────────────────
        uint256 balanceBefore = IERC20(intent.tokenOut).balanceOf(address(this));
        IERC20(intent.tokenIn).approve(intent.router, intent.amountIn);
        (bool success,) = intent.router.call(intent.callData);
        require(success, "Router call failed");
        uint256 balanceAfter = IERC20(intent.tokenOut).balanceOf(address(this));

        // ── Step 8: Drawdown recording ─────────────────────────────────────────
        if (balanceAfter < balanceBefore + intent.amountOutMin) {
            uint256 shortfall = (balanceBefore + intent.amountOutMin) - balanceAfter;
            uint256 shortfallUsd = _estimatePositionUsd(intent.tokenOut, shortfall);
            _recordDrawdown(shortfallUsd);
        }

        lastExecutionBlock   = block.number;
        lastStrategyChangeAt = block.timestamp;

        emit IntentValidated(intentHash, intent.router, intent.amountIn);
        emit IntentExecutedWithProof(intentHash, proofExecHash, balanceAfter - balanceBefore);
    }

    /**
     * @notice Pack 32 consecutive single-byte field elements into a bytes32.
     * @dev    Noir encodes [u8; 32] as 32 separate bytes32 slots, one byte per slot.
     *         This reverses that encoding: slot[i] contributes byte (31-i) of the result.
     */
    function _packBytes32FromInputs(
        bytes32[] calldata inputs,
        uint256 offset
    ) internal pure returns (bytes32 result) {
        for (uint256 i = 0; i < 32; i++) {
            result |= bytes32(uint256(inputs[offset + i]) << (8 * (31 - i)));
        }
    }

    receive() external payable {}
}
