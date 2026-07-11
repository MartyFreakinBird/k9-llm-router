// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

/**
 * @title  DeployAEG6
 * @notice AEG-6 — Full Sentry Kernel on-chain deployment
 *
 *         Deploys in one atomic broadcast:
 *           1. ProofOfRationalityVerifier  (AEG-9 Ultra Honk SNARK verifier)
 *           2. SessionKeyWallet  (AEG-7 risk-guarded ERC-4337 wallet)
 *           3. Wires verifier address into SessionKeyWallet
 *
 * Usage (Base Sepolia):
 *   forge script contracts/script/DeployAEG6.s.sol:DeployAEG6 \
 *     --rpc-url $BASE_SEPOLIA_RPC \
 *     --broadcast \
 *     --verify \
 *     --etherscan-api-key $BASESCAN_API_KEY \
 *     -vvvv
 *
 * Required env vars (set in .env, never commit):
 *   DEPLOYER_PK            private key of deploy wallet
 *   OWNER_ADDRESS          multisig or EOA that owns the wallet
 *   SESSION_KEY_ADDRESS    K-9 AI signing key (:8769 sovereign kernel)
 *   AEG_TREASURY_ADDRESS   AEGTreasury from AEG-4 deploy
 *   AEG_GOVERNOR_ADDRESS   AEGGovernor from AEG-4 deploy
 *   BASESCAN_API_KEY       for --verify (get from basescan.org)
 *
 * Base Sepolia constants (public, hardcoded):
 *   ETH/USD Chainlink feed : 0x4aDC67696bA383F43DD60A9e78F2C97Fbbfc7cb1
 *   ERC-4337 EntryPoint    : 0x5FF137D4b0FDCD49DcA30c7CF57E578a026d2789
 */

import {Script, console2} from "forge-std/Script.sol";
import {SessionKeyWallet}           from "../src/SessionKeyWallet.sol";
import {HonkVerifier as ProofOfRationalityVerifier} from "../src/ProofOfRationalityVerifier.sol";

contract DeployAEG6 is Script {

    address constant ETH_USD_FEED = 0x4aDC67696bA383F43DD60A9e78F2C97Fbbfc7cb1;
    address constant ENTRY_POINT  = 0x5FF137D4b0FDCD49DcA30c7CF57E578a026d2789;

    struct Deployment {
        address wallet;
        address verifier;
        bytes32 paramsHash;
    }

    function run() external returns (Deployment memory d) {

        uint256 deployerPk = vm.envUint("DEPLOYER_PK");
        address owner      = vm.envAddress("OWNER_ADDRESS");
        address sessionKey = vm.envAddress("SESSION_KEY_ADDRESS");
        address treasury   = vm.envAddress("AEG_TREASURY_ADDRESS");
        address governor   = vm.envAddress("AEG_GOVERNOR_ADDRESS");

        vm.startBroadcast(deployerPk);

        // ── Step 1: Deploy ProofOfRationalityVerifier ─────────────────────────
        ProofOfRationalityVerifier verifier = new ProofOfRationalityVerifier();
        d.verifier = address(verifier);

        console2.log("=== [1/3] ProofOfRationalityVerifier DEPLOYED ===");
        console2.log("Address:");
        console2.log(d.verifier);

        // ── Step 2: Deploy SessionKeyWallet ───────────────────────────────────
        SessionKeyWallet wallet = new SessionKeyWallet(
            owner,
            sessionKey,
            treasury,
            ETH_USD_FEED,
            ENTRY_POINT,
            governor
        );
        d.wallet     = address(wallet);
        d.paramsHash = wallet.currentParamsHash();

        console2.log("=== [2/3] SessionKeyWallet DEPLOYED ===");
        console2.log("Address:");
        console2.log(d.wallet);
        console2.log("Owner:");
        console2.log(owner);
        console2.log("Session Key:");
        console2.log(sessionKey);
        console2.log("Params Hash:");
        console2.logBytes32(d.paramsHash);

        // ── Step 3: Wire verifier into wallet ─────────────────────────────────
        wallet.setVerifier(d.verifier);

        console2.log("=== [3/3] Verifier wired ===");
        console2.log("wallet.verifier():");
        console2.log(wallet.verifier());

        vm.stopBroadcast();

        // ── Deployment manifest ───────────────────────────────────────────────
        console2.log("");
        console2.log("======================================================");
        console2.log("       AEG-6 SENTRY KERNEL - DEPLOYED                 ");
        console2.log("======================================================");
        console2.log("SessionKeyWallet:");
        console2.log(d.wallet);
        console2.log("ProofOfRationalityVerifier:");
        console2.log(d.verifier);
        console2.log("Params Hash:");
        console2.logBytes32(d.paramsHash);
        console2.log("------------------------------------------------------");
        console2.log("Genesis Risk Parameters:");
        console2.log("  maxDrawdownBps      = 150  (1.5%)");
        console2.log("  maxPoolShareBps     = 200  (2.0%)");
        console2.log("  maxPositionUsd      = $25k");
        console2.log("  slippageBlueChipBps = 30   (0.30%)");
        console2.log("  slippageAltBps      = 75   (0.75%)");
        console2.log("  maxGasUsd           = $3.00");
        console2.log("  cooldownSeconds     = 60");
        console2.log("ZK Circuit: AEG-9 Ultra Honk (6 invariants)");
        console2.log("======================================================");
        console2.log("");
        console2.log("NEXT STEPS:");
        console2.log("  export SESSION_WALLET_ADDRESS=");
        console2.log(d.wallet);
        console2.log("  export VERIFIER_ADDRESS=");
        console2.log(d.verifier);
        console2.log("  Update src/k9_proof_shim.py with both addresses");
        console2.log("  Verify on Basescan: https://sepolia.basescan.org/address/");
        console2.log(d.wallet);
    }
}
