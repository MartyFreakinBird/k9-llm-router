// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

/**
 * @title  DeployAEG7
 * @notice Foundry deploy script — AEG-7 SessionKeyWallet
 *
 * Usage (Base Sepolia):
 *   forge script contracts/script/DeployAEG7.s.sol:DeployAEG7 \
 *     --rpc-url $BASE_SEPOLIA_RPC \
 *     --broadcast \
 *     --verify \
 *     -vvvv
 *
 * Env vars required:
 *   DEPLOYER_PK          — private key (never commit)
 *   OWNER_ADDRESS        — multisig or EOA owner
 *   SESSION_KEY_ADDRESS  — K-9 local AI signing key
 *   AEG_TREASURY_ADDRESS — AEGTreasury.sol (from AEG-4 deploy)
 *   AEG_GOVERNOR_ADDRESS — AEGGovernor.sol (from AEG-4 deploy)
 *
 * Base Sepolia Chainlink ETH/USD feed: 0x4aDC67696bA383F43DD60A9e78F2C97Fbbfc7cb1
 * Base Sepolia ERC-4337 EntryPoint:    0x5FF137D4b0FDCD49DcA30c7CF57E578a026d2789
 */

import {Script, console2} from "forge-std/Script.sol";
import {SessionKeyWallet} from "../src/SessionKeyWallet.sol";

contract DeployAEG7 is Script {

    // ── Base Sepolia addresses ─────────────────────────────────────────────────
    address constant ETH_USD_FEED_SEPOLIA =
        0x4aDC67696bA383F43DD60A9e78F2C97Fbbfc7cb1;

    address constant ENTRY_POINT_SEPOLIA =
        0x5FF137D4b0FDCD49DcA30c7CF57E578a026d2789;

    function run() external {
        uint256 deployerPk      = vm.envUint("DEPLOYER_PK");
        address owner           = vm.envAddress("OWNER_ADDRESS");
        address sessionKey      = vm.envAddress("SESSION_KEY_ADDRESS");
        address treasury        = vm.envAddress("AEG_TREASURY_ADDRESS");
        address governor        = vm.envAddress("AEG_GOVERNOR_ADDRESS");

        vm.startBroadcast(deployerPk);

        SessionKeyWallet wallet = new SessionKeyWallet(
            owner,
            sessionKey,
            treasury,
            ETH_USD_FEED_SEPOLIA,
            ENTRY_POINT_SEPOLIA,
            governor
        );

        console2.log("=== AEG-7 SessionKeyWallet DEPLOYED ===");
        console2.log("Address:        ", address(wallet));
        console2.log("Owner:          ", owner);
        console2.log("Session Key:    ", sessionKey);
        console2.log("Treasury:       ", treasury);
        console2.log("Governor:       ", governor);
        console2.log("Genesis Lock:   ", wallet.deployedAt() + 30 days);
        console2.log("Params Hash:    ");
        console2.logBytes32(wallet.currentParamsHash());
        console2.log("");
        console2.log("Genesis Risk Parameters:");
        console2.log("  maxDrawdownBps:      150 (1.5%)");
        console2.log("  maxPoolShareBps:     200 (2.0%)");
        console2.log("  maxPositionUsd:      25000 USDC");
        console2.log("  slippageBlueChipBps: 30 (0.30%)");
        console2.log("  slippageAltBps:      75 (0.75%)");
        console2.log("  maxGasUsd:           $3.00");
        console2.log("  cooldownSeconds:     60s");
        console2.log("  whitelistedRouters:  Uniswap V3, Aave V3, Curve, Balancer V2");

        vm.stopBroadcast();
    }
}
