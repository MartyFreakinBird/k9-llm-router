import { ethers } from 'ethers';
import * as dotenv from 'dotenv';

dotenv.config();

const RPC_URL = process.env.BASE_SEPOLIA_RPC || 'https://sepolia.base.org';

// Standard Chainlink AggregatorV3 ABI
const AGGREGATOR_V3_ABI = [
  {
    inputs: [],
    name: 'decimals',
    outputs: [{ internalType: 'uint8', name: '', type: 'uint8' }],
    stateMutability: 'view',
    type: 'function',
  },
  {
    inputs: [],
    name: 'latestRoundData',
    outputs: [
      { internalType: 'uint80', name: 'roundId', type: 'uint80' },
      { internalType: 'int256', name: 'answer', type: 'int256' },
      { internalType: 'uint256', name: 'startedAt', type: 'uint256' },
      { internalType: 'uint256', name: 'updatedAt', type: 'uint256' },
      { internalType: 'uint80', name: 'answeredInRound', type: 'uint80' },
    ],
    stateMutability: 'view',
    type: 'function',
  },
];

// Map of asset pairs to Chainlink Price Feed Addresses on Base Sepolia
// If not available, we use a default fallback or simulated feed.
const FEED_ADDRESSES: { [pair: string]: string } = {
  'ETH/USD': '0x4aDC67696bA3d23901b02924ec6D5146de3CE50C',
  'BTC/USD': '0x0fb99723Aee6f420beAD13e6bBB79b7E6F034298',
  'zKRW/zGOLD': '0x4aDC67696bA3d23901b02924ec6D5146de3CE50C', // Fallback to ETH/USD for demo/testing
};

export class ChainlinkOracle {
  private provider: ethers.JsonRpcProvider | null = null;

  constructor() {
    try {
      this.provider = new ethers.JsonRpcProvider(RPC_URL);
    } catch (error) {
      console.warn('Oracle: Failed to initialize JSON-RPC provider, using simulated feeds.', error);
    }
  }

  /**
   * Fetches the latest price for a given pair (e.g. "zKRW/zGOLD" or "ETH/USD").
   * Returns price as a float.
   */
  public async getLatestPrice(pair: string): Promise<number> {
    const address = FEED_ADDRESSES[pair] || FEED_ADDRESSES['ETH/USD'];

    if (!this.provider) {
      return this.getSimulatedPrice(pair);
    }

    try {
      const contract = new ethers.Contract(address, AGGREGATOR_V3_ABI, this.provider);
      
      // Call latestRoundData and decimals in parallel
      const [roundData, decimals] = await Promise.all([
        contract.latestRoundData(),
        contract.decimals(),
      ]);

      const price = Number(roundData.answer) / Math.pow(10, Number(decimals));
      return price;
    } catch (error) {
      console.warn(`Oracle: Error fetching live price for ${pair} from on-chain aggregator, falling back to simulated price:`, error);
      return this.getSimulatedPrice(pair);
    }
  }

  /**
   * Generates a stable simulated price for development or fallback use.
   */
  private getSimulatedPrice(pair: string): number {
    switch (pair.toUpperCase()) {
      case 'BTC/USD':
        return 65000.0 + (Math.random() - 0.5) * 10;
      case 'ETH/USD':
        return 3400.0 + (Math.random() - 0.5) * 2;
      case 'ZKRW/ZGOLD':
        // Let's say 1 zGOLD is 2500 zKRW
        return 2500.0 + (Math.random() - 0.5) * 1;
      default:
        return 1.0;
    }
  }
}
