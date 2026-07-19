// SPDX-License-Identifier: MIT
pragma solidity 0.8.27;

import {Ownable} from "@openzeppelin/contracts/access/Ownable.sol";

interface AggregatorV3Interface {
    function decimals() external view returns (uint8);
    function description() external view returns (string memory);
    function version() external view returns (uint256);
    function getRoundData(uint80 _roundId) external view returns (
        uint80 roundId,
        int256 answer,
        uint256 startedAt,
        uint256 updatedAt,
        uint80 answeredInRound
    );
    function latestRoundData() external view returns (
        uint80 roundId,
        int256 answer,
        uint256 startedAt,
        uint256 updatedAt,
        uint80 answeredInRound
    );
}

contract PriceOracle is Ownable {
    mapping(address => address) public feeds;
    uint256 public stalePeriod = 24 hours;

    event FeedSet(address indexed token, address indexed feed);
    event StalePeriodUpdated(uint256 newPeriod);

    constructor() Ownable(msg.sender) {}

    function setFeed(address token, address feed) external onlyOwner {
        require(token != address(0), "PriceOracle: token cannot be zero");
        require(feed != address(0), "PriceOracle: feed cannot be zero");
        feeds[token] = feed;
        emit FeedSet(token, feed);
    }

    function setStalePeriod(uint256 newPeriod) external onlyOwner {
        require(newPeriod > 0, "PriceOracle: stale period must be > 0");
        stalePeriod = newPeriod;
        emit StalePeriodUpdated(newPeriod);
    }

    function getPrice(address token) external view returns (uint256) {
        address feedAddress = feeds[token];
        require(feedAddress != address(0), "PriceOracle: no feed for token");
        AggregatorV3Interface feed = AggregatorV3Interface(feedAddress);

        uint80 latestRound;
        int256 latestPrice;
        uint256 latestUpdatedAt;
        bool latestValid = false;

        try feed.latestRoundData() returns (
            uint80 roundId,
            int256 answer,
            uint256 /*startedAt*/,
            uint256 updatedAt,
            uint80 /*answeredInRound*/
        ) {
            latestRound = roundId;
            latestPrice = answer;
            latestUpdatedAt = updatedAt;
            if (answer > 0 && updatedAt != 0 && (block.timestamp - updatedAt) <= stalePeriod) {
                latestValid = true;
            }
        } catch {}

        if (latestValid) {
            return _scalePrice(latestPrice, feed.decimals());
        }

        // Fallback to TWAP of last 5 rounds
        require(latestRound > 0, "PriceOracle: invalid latest round ID");
        
        uint256 sumPrice = 0;
        uint256 validCount = 0;
        uint80 currentRound = latestRound;

        for (uint256 i = 0; i < 5; i++) {
            if (currentRound == 0) {
                break;
            }
            try feed.getRoundData(currentRound) returns (
                uint80 /*rId*/,
                int256 answer,
                uint256 /*startedAt*/,
                uint256 updatedAt,
                uint80 /*answeredInRound*/
            ) {
                if (answer > 0 && updatedAt != 0) {
                    sumPrice += uint256(answer);
                    validCount++;
                }
            } catch {}
            currentRound--;
        }

        require(validCount > 0, "PriceOracle: no valid round data for fallback");
        uint256 avgPrice = sumPrice / validCount;
        return _scalePrice(int256(avgPrice), feed.decimals());
    }

    function _scalePrice(int256 price, uint8 decimals) internal pure returns (uint256) {
        require(price > 0, "PriceOracle: price must be positive");
        uint256 uPrice = uint256(price);
        if (decimals < 18) {
            return uPrice * (10 ** (18 - decimals));
        } else if (decimals > 18) {
            return uPrice / (10 ** (decimals - 18));
        } else {
            return uPrice;
        }
    }
}
