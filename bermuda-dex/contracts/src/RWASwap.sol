// SPDX-License-Identifier: MIT
pragma solidity 0.8.27;

import {Ownable} from "@openzeppelin/contracts/access/Ownable.sol";
import {ReentrancyGuard} from "@openzeppelin/contracts/utils/ReentrancyGuard.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import {ICompliance} from "./ICompliance.sol";
import {ComplianceFeeCollector} from "./ComplianceFeeCollector.sol";

contract RWASwap is Ownable, ReentrancyGuard {
    using SafeERC20 for IERC20;

    struct Swap {
        address initiator;
        address tokenA;
        uint256 amountA;
        uint256 netAmountA;
        address tokenB;
        uint256 amountB;
        uint256 expiry;
        bytes32 hashlock;
        bytes32 secret;
        bool claimed;
        bool refunded;
    }

    ICompliance public immutable compliancePassport;
    ComplianceFeeCollector public immutable feeCollector;

    mapping(address => bool) public isWhitelisted;
    mapping(bytes32 => Swap) public swaps;

    event SwapOpened(
        bytes32 indexed swapId,
        address indexed initiator,
        address tokenA,
        uint256 amountA,
        address tokenB,
        uint256 amountB,
        uint256 expiry,
        bytes32 hashlock
    );
    event SwapClaimed(bytes32 indexed swapId, bytes32 secret);
    event SwapRefunded(bytes32 indexed swapId);
    event TokenWhitelistStatusUpdated(address indexed token, bool status);

    modifier onlyWhitelisted(address token) {
        require(isWhitelisted[token], "RWASwap: token not whitelisted");
        _;
    }

    modifier validKYC(address user) {
        require(compliancePassport.hasValidPassport(user), "RWASwap: user lacks valid KYC passport");
        _;
    }

    constructor(
        address _compliancePassport,
        address payable _feeCollector
    ) Ownable(msg.sender) {
        require(_compliancePassport != address(0), "RWASwap: zero compliance address");
        require(_feeCollector != address(0), "RWASwap: zero fee collector address");
        compliancePassport = ICompliance(_compliancePassport);
        feeCollector = ComplianceFeeCollector(_feeCollector);
    }

    function setTokenWhitelist(address token, bool status) external onlyOwner {
        require(token != address(0), "RWASwap: zero address token");
        isWhitelisted[token] = status;
        emit TokenWhitelistStatusUpdated(token, status);
    }

    function openSwap(
        address tokenA,
        uint256 amountA,
        address tokenB,
        uint256 amountB,
        uint256 expiry,
        bytes32 hashlock
    )
        external
        nonReentrant
        onlyWhitelisted(tokenA)
        onlyWhitelisted(tokenB)
        validKYC(msg.sender)
        returns (bytes32 swapId)
    {
        require(expiry > block.timestamp, "RWASwap: expiry must be in the future");
        require(amountA > 0, "RWASwap: amountA must be > 0");
        require(amountB > 0, "RWASwap: amountB must be > 0");
        require(hashlock != bytes32(0), "RWASwap: invalid hashlock");

        uint256 feeBps = feeCollector.complianceFeeBps();
        uint256 fee = (amountA * feeBps) / 10000;
        uint256 netAmountA = amountA - fee;

        swapId = keccak256(
            abi.encodePacked(
                msg.sender,
                tokenA,
                amountA,
                tokenB,
                amountB,
                expiry,
                hashlock
            )
        );

        require(swaps[swapId].initiator == address(0), "RWASwap: swap already exists");

        swaps[swapId] = Swap({
            initiator: msg.sender,
            tokenA: tokenA,
            amountA: amountA,
            netAmountA: netAmountA,
            tokenB: tokenB,
            amountB: amountB,
            expiry: expiry,
            hashlock: hashlock,
            secret: bytes32(0),
            claimed: false,
            refunded: false
        });

        emit SwapOpened(
            swapId,
            msg.sender,
            tokenA,
            amountA,
            tokenB,
            amountB,
            expiry,
            hashlock
        );

        // Perform transfers (CEI enforced)
        IERC20(tokenA).safeTransferFrom(msg.sender, address(this), amountA);
        if (fee > 0) {
            IERC20(tokenA).safeTransfer(address(feeCollector), fee);
        }
    }

    function claimSwap(
        bytes32 swapId,
        bytes32 secret
    )
        external
        nonReentrant
        validKYC(msg.sender)
    {
        Swap storage swap = swaps[swapId];
        require(swap.initiator != address(0), "RWASwap: swap does not exist");
        require(!swap.claimed, "RWASwap: swap already claimed");
        require(!swap.refunded, "RWASwap: swap already refunded");
        require(block.timestamp <= swap.expiry, "RWASwap: swap has expired");
        require(keccak256(abi.encodePacked(secret)) == swap.hashlock, "RWASwap: invalid secret");

        swap.claimed = true;
        swap.secret = secret;

        emit SwapClaimed(swapId, secret);

        // Perform transfers (CEI enforced)
        IERC20(swap.tokenB).safeTransferFrom(msg.sender, swap.initiator, swap.amountB);
        IERC20(swap.tokenA).safeTransfer(msg.sender, swap.netAmountA);
    }

    function refund(
        bytes32 swapId
    )
        external
        nonReentrant
    {
        Swap storage swap = swaps[swapId];
        require(swap.initiator != address(0), "RWASwap: swap does not exist");
        require(!swap.claimed, "RWASwap: swap already claimed");
        require(!swap.refunded, "RWASwap: swap already refunded");
        require(block.timestamp > swap.expiry, "RWASwap: swap has not expired");

        swap.refunded = true;

        emit SwapRefunded(swapId);

        // Perform transfers (CEI enforced)
        IERC20(swap.tokenA).safeTransfer(swap.initiator, swap.netAmountA);
    }
}
