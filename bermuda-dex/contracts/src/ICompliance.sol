// SPDX-License-Identifier: MIT
pragma solidity 0.8.27;

interface ICompliance {
    function hasValidPassport(address user) external view returns (bool);
}
