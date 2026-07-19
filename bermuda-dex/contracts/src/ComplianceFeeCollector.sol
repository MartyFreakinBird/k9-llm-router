// SPDX-License-Identifier: MIT
pragma solidity 0.8.27;

import {Ownable} from "@openzeppelin/contracts/access/Ownable.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";

contract ComplianceFeeCollector is Ownable {
    using SafeERC20 for IERC20;

    uint256 public complianceFeeBps = 15; // default 15 basis points (0.15%)
    uint256 public constant MAX_FEE_BPS = 500; // cap at 5% for sanity

    event ComplianceFeeBpsUpdated(uint256 oldBps, uint256 newBps);
    event TokensWithdrawn(address indexed token, address indexed to, uint256 amount);
    event EtherWithdrawn(address indexed to, uint256 amount);

    constructor() Ownable(msg.sender) {}

    function setComplianceFeeBps(uint256 newBps) external onlyOwner {
        require(newBps <= MAX_FEE_BPS, "ComplianceFeeCollector: fee exceeds max cap");
        uint256 oldBps = complianceFeeBps;
        complianceFeeBps = newBps;
        emit ComplianceFeeBpsUpdated(oldBps, newBps);
    }

    function withdrawToken(address token, address to, uint256 amount) external onlyOwner {
        require(to != address(0), "ComplianceFeeCollector: transfer to zero address");
        require(amount > 0, "ComplianceFeeCollector: amount must be > 0");
        
        IERC20(token).safeTransfer(to, amount);
        emit TokensWithdrawn(token, to, amount);
    }

    function withdrawEther(address payable to, uint256 amount) external onlyOwner {
        require(to != address(0), "ComplianceFeeCollector: transfer to zero address");
        require(amount > 0, "ComplianceFeeCollector: amount must be > 0");
        require(address(this).balance >= amount, "ComplianceFeeCollector: insufficient balance");

        (bool success, ) = to.call{value: amount}("");
        require(success, "ComplianceFeeCollector: ether transfer failed");
        emit EtherWithdrawn(to, amount);
    }

    receive() external payable {}
}
