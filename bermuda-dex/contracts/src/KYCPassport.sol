// SPDX-License-Identifier: MIT
pragma solidity 0.8.27;

import {ERC721} from "@openzeppelin/contracts/token/ERC721/ERC721.sol";
import {Ownable} from "@openzeppelin/contracts/access/Ownable.sol";

contract KYCPassport is ERC721, Ownable {
    uint256 private _nextTokenId;

    // Mapping from user address to their active passport token ID
    mapping(address => uint256) public userPassportId;
    
    // Mapping from user address to validity status
    mapping(address => bool) private _isValid;

    event PassportIssued(address indexed user, uint256 tokenId);
    event PassportRevoked(address indexed user, uint256 tokenId);

    constructor() ERC721("KYC Passport", "KYCP") Ownable(msg.sender) {}

    function issuePassport(address user) external onlyOwner returns (uint256) {
        require(user != address(0), "KYCPassport: invalid address");
        require(!_isValid[user], "KYCPassport: passport already active");

        _nextTokenId++;
        uint256 tokenId = _nextTokenId;

        userPassportId[user] = tokenId;
        _isValid[user] = true;

        _safeMint(user, tokenId);

        emit PassportIssued(user, tokenId);
        return tokenId;
    }

    function revokePassport(address user) external onlyOwner {
        require(_isValid[user], "KYCPassport: no active passport");

        uint256 tokenId = userPassportId[user];
        _isValid[user] = false;

        _burn(tokenId);

        emit PassportRevoked(user, tokenId);
    }

    function hasValidPassport(address user) external view returns (bool) {
        return _isValid[user];
    }

    // Override _update to make the passport non-transferable (only allow minting and burning)
    function _update(address to, uint256 tokenId, address auth) internal override returns (address) {
        address previousOwner = super._update(to, tokenId, auth);
        if (previousOwner != address(0) && to != address(0)) {
            revert("KYCPassport: non-transferable");
        }
        return previousOwner;
    }
}
