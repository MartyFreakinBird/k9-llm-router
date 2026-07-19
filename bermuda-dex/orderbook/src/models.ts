export type OrderSide = 'BUY' | 'SELL';

export type OrderStatus = 'OPEN' | 'PARTIALLY_FILLED' | 'FILLED' | 'CANCELLED';

export interface Order {
  id: string;
  pair: string; // e.g., 'zKRW/zGOLD'
  side: OrderSide;
  price: number;
  amount: number;
  filledAmount: number;
  walletAddress: string;
  status: OrderStatus;
  timestamp: number;
}

export interface Trade {
  id: string;
  pair: string;
  buyOrderId: string;
  sellOrderId: string;
  price: number;
  amount: number;
  timestamp: number;
  hashlock: string;
  secret: string;
}

export interface SwapIntent {
  tradeId: string;
  initiator: string;
  recipient: string;
  amount: number;
  hashlock: string;
  timelock: number;
  secret: string;
}
