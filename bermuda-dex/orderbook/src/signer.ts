import { ethers } from 'ethers';
import { OnChainRelayer } from './onchain';
import { Trade } from './models';
import * as dotenv from 'dotenv';

dotenv.config();

const PRIVATE_KEY = process.env.RELAYER_PRIVATE_KEY;

export class CLOBSigner {
  private wallet: ethers.Wallet | null = null;
  private relayer: OnChainRelayer;

  constructor() {
    this.relayer = new OnChainRelayer();
    try {
      if (PRIVATE_KEY && PRIVATE_KEY !== '0x0000000000000000000000000000000000000000000000000000000000000000') {
        this.wallet = new ethers.Wallet(PRIVATE_KEY);
      }
    } catch (error) {
      console.error('CLOBSigner: Failed to initialize signer wallet:', error);
    }
  }

  /**
   * Generates a random 32-byte secret and its keccak256 hashlock.
   */
  public generateHashlock(): { secret: string; hashlock: string } {
    const secretBytes = ethers.randomBytes(32);
    const secret = ethers.hexlify(secretBytes);
    const hashlock = ethers.keccak256(secret);
    return { secret, hashlock };
  }

  /**
   * Signs the match payload to prove that the CLOB engine authorized this match.
   */
  public async signMatch(
    buyOrderId: string,
    sellOrderId: string,
    price: number,
    amount: number,
    hashlock: string
  ): Promise<string> {
    if (!this.wallet) {
      console.warn('CLOBSigner [SIMULATION]: No signer wallet initialized. Returning mock signature.');
      return '0xmock_signature_' + Math.random().toString(36).substr(2, 9);
    }

    try {
      // Hash the matching parameters using ethers.solidityPackedKeccak256
      const messageHash = ethers.solidityPackedKeccak256(
        ['string', 'string', 'uint256', 'uint256', 'bytes32'],
        [
          buyOrderId,
          sellOrderId,
          ethers.parseUnits(price.toString(), 18),
          ethers.parseUnits(amount.toString(), 18),
          hashlock
        ]
      );

      // Sign the message hash
      const signature = await this.wallet.signMessage(ethers.getBytes(messageHash));
      return signature;
    } catch (error) {
      console.error('CLOBSigner: Failed to sign match:', error);
      return '0xfailed_signature';
    }
  }

  /**
   * Main integration method called after a match is executed by the CLOB engine.
   * Generates the hashlock, signs the match, and opens the swap on-chain.
   */
  public async processTradeAndOpenSwap(
    trade: Trade,
    initiatorAddress: string,
    recipientAddress: string
  ): Promise<{ secret: string; hashlock: string; txHash: string; signature: string }> {
    // 1. Generate random secret & hashlock
    const { secret, hashlock } = this.generateHashlock();

    // 2. Update the trade object in-place or return updated values
    trade.secret = secret;
    trade.hashlock = hashlock;

    // 3. Sign the match
    const signature = await this.signMatch(
      trade.buyOrderId,
      trade.sellOrderId,
      trade.price,
      trade.amount,
      hashlock
    );

    // 4. Determine timelock (e.g. 1 hour from now)
    const ONE_HOUR_IN_SECONDS = 3600;
    const timelock = Math.floor(Date.now() / 1000) + ONE_HOUR_IN_SECONDS;

    // 5. Trigger onchain.ts openSwap
    const txHash = await this.relayer.openSwap(
      trade.id,
      initiatorAddress,
      recipientAddress,
      trade.amount,
      hashlock,
      timelock
    );

    return { secret, hashlock, txHash, signature };
  }
}
