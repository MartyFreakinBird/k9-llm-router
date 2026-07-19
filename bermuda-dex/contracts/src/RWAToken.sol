// SPDX-License-Identifier: MIT
pragma solidity 0.8.27;

import {ERC20} from "@openzeppelin/contracts/token/ERC20/ERC20.sol";
import {Ownable} from "@openzeppelin/contracts/access/Ownable.sol";
import {ICompliance} from "./ICompliance.sol";

contract RWAToken is ERC20, Ownable {
    ICompliance public immutable compliancePassport;

    constructor(
        string memory name,
        string memory symbol,
        uint256 initialSupply,
        address initialOwner,
        address _compliancePassport
    ) ERC20(name, symbol) Ownable(initialOwner) {
        require(_compliancePassport != address(0), "RWAToken: zero address passport");
        compliancePassport = ICompliance(_compliancePassport);
        
        if (initialSupply > 0) {
            // If the recipient (initialOwner) needs to have a passport, they must be issued one first.
            // However, to make deployment and testing smoother, we can bypass KYC check during constructor minting
            // or we can require them to have one. Let's write the override in a way that checks KYC for normal transfers,
            // but let's be strict or lenient. Let's check: if we allow minting without KYC, is it better?
            // "blocks transfers if recipient lacks KYC passport"
            // Usually, minting is from address(0), and burns are to address(0).
            // Let's check KYC for transfers, meaning from != address(0) && to != address(0).
            // If from == address(0), it is a mint, maybe we check or not. Let's make it check if from != address(0) && to != address(0).
            // Wait! If the spec says "blocks transfers if recipient lacks KYC passport", standard transfers are transfer() and transferFrom().
            // In ERC20, both invoke _update(from, to, amount).
            // If we enforce KYC only when `from != address(0)` (so not mint) and `to != address(0)` (so not burn), it allows the owner/system to mint freely,
            // while guarding all user-to-user and DEX-to-user transfers. This is incredibly practical and robust!
            // Let's implement this logic:
        }
    }

    function mint(address to, uint256 amount) external onlyOwner {
        _mint(to, amount);
    }

    function burn(address from, uint256 amount) external onlyOwner {
        _burn(from, amount);
    }

    function _update(address from, address to, uint256 value) internal override {
        // Enforce KYC on recipient for transfers (not mints or burns)
        if (from != address(0) && to != address(0)) {
            require(
                compliancePassport.hasValidPassport(to),
                "RWAToken: recipient lacks valid KYC passport"
            );
        }
        super._update(from, to, value);
    }
}
