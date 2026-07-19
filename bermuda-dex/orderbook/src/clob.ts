import { Order, Trade } from './models';

export class CLOBEngine {
  // Order book stored in memory (Map<string, Order[]> keyed by pair e.g. 'zKRW/zGOLD')
  private orders: Map<string, Order[]> = new Map();
  // Store all completed trades keyed by pair
  private trades: Map<string, Trade[]> = new Map();

  constructor() {}

  /**
   * Adds a new limit order to the order book.
   */
  public addOrder(order: Order): void {
    if (!this.orders.has(order.pair)) {
      this.orders.set(order.pair, []);
    }
    this.orders.get(order.pair)!.push(order);
  }

  /**
   * Cancels an existing order by ID.
   */
  public cancelOrder(orderId: string): boolean {
    for (const [pair, orderList] of this.orders.entries()) {
      const idx = orderList.findIndex(o => o.id === orderId);
      if (idx !== -1) {
        const order = orderList[idx];
        if (order.status === 'OPEN' || order.status === 'PARTIALLY_FILLED') {
          order.status = 'CANCELLED';
          // Keep cancelled orders in the array but they won't match, or filter them out.
          return true;
        }
      }
    }
    return false;
  }

  /**
   * Gets the list of trades for a specific pair.
   */
  public getTrades(pair: string): Trade[] {
    return this.trades.get(pair) || [];
  }

  /**
   * Adds trades to the list for a pair.
   */
  public addTrades(pair: string, newTrades: Trade[]): void {
    if (!this.trades.has(pair)) {
      this.trades.set(pair, []);
    }
    const pairTrades = this.trades.get(pair)!;
    pairTrades.push(...newTrades);
    // Keep only last 1000 trades in memory to avoid bloat, while REST endpoint returns last 50
    if (pairTrades.length > 1000) {
      this.trades.set(pair, pairTrades.slice(-1000));
    }
  }

  /**
   * Runs the price-time priority matching engine for a given pair.
   * Returns an array of Trades executed during this matching run.
   */
  public match(pair: string): Trade[] {
    const allOrders = this.orders.get(pair) || [];
    
    // Filter active orders
    const activeOrders = allOrders.filter(
      o => o.status === 'OPEN' || o.status === 'PARTIALLY_FILLED'
    );

    // Separate bids and asks
    const bids = activeOrders.filter(o => o.side === 'BUY');
    const asks = activeOrders.filter(o => o.side === 'SELL');

    // Sort bids: Price descending, then Timestamp ascending
    bids.sort((a, b) => {
      if (b.price !== a.price) {
        return b.price - a.price;
      }
      return a.timestamp - b.timestamp;
    });

    // Sort asks: Price ascending, then Timestamp ascending
    asks.sort((a, b) => {
      if (a.price !== b.price) {
        return a.price - b.price;
      }
      return a.timestamp - b.timestamp;
    });

    const matches: Trade[] = [];

    let bidIdx = 0;
    let askIdx = 0;

    while (bidIdx < bids.length && askIdx < asks.length) {
      const bid = bids[bidIdx];
      const ask = asks[askIdx];

      // Match condition: highest bid price >= lowest ask price
      if (bid.price >= ask.price) {
        // Price-time priority: maker price rules. The order placed earlier is the maker.
        const makerPrice = bid.timestamp < ask.timestamp ? bid.price : ask.price;

        const bidRemaining = bid.amount - bid.filledAmount;
        const askRemaining = ask.amount - ask.filledAmount;
        const matchAmount = Math.min(bidRemaining, askRemaining);

        // Update filled amounts
        bid.filledAmount += matchAmount;
        ask.filledAmount += matchAmount;

        // Update statuses
        bid.status = bid.filledAmount === bid.amount ? 'FILLED' : 'PARTIALLY_FILLED';
        ask.status = ask.filledAmount === ask.amount ? 'FILLED' : 'PARTIALLY_FILLED';

        // Generate trade ID
        const tradeId = 'trade_' + Math.random().toString(36).substr(2, 9);

        const trade: Trade = {
          id: tradeId,
          pair,
          buyOrderId: bid.id,
          sellOrderId: ask.id,
          price: makerPrice,
          amount: matchAmount,
          timestamp: Date.now(),
          hashlock: '', // Will be filled by signer
          secret: ''    // Will be filled by signer
        };

        matches.push(trade);

        // Advance indices if fully filled
        if (bid.status === 'FILLED') {
          bidIdx++;
        }
        if (ask.status === 'FILLED') {
          askIdx++;
        }
      } else {
        // No matching bids and asks anymore
        break;
      }
    }

    // Update orders in the primary list (filtering out filled/cancelled is optional, but we keep them for history)
    this.addTrades(pair, matches);

    return matches;
  }

  /**
   * Returns L2 order book: aggregated buy/sell volume at each price level, top 10 each.
   */
  public getOrderBook(pair: string) {
    const allOrders = this.orders.get(pair) || [];
    const activeOrders = allOrders.filter(
      o => o.status === 'OPEN' || o.status === 'PARTIALLY_FILLED'
    );

    const bids = activeOrders.filter(o => o.side === 'BUY');
    const asks = activeOrders.filter(o => o.side === 'SELL');

    // Aggregate bids by price
    const bidMap: { [price: number]: number } = {};
    for (const bid of bids) {
      const remaining = bid.amount - bid.filledAmount;
      if (remaining > 0) {
        bidMap[bid.price] = (bidMap[bid.price] || 0) + remaining;
      }
    }

    // Aggregate asks by price
    const askMap: { [price: number]: number } = {};
    for (const ask of asks) {
      const remaining = ask.amount - ask.filledAmount;
      if (remaining > 0) {
        askMap[ask.price] = (askMap[ask.price] || 0) + remaining;
      }
    }

    // Format and sort bids (price descending)
    const aggregatedBids = Object.keys(bidMap)
      .map(priceStr => ({
        price: Number(priceStr),
        amount: bidMap[Number(priceStr)]
      }))
      .sort((a, b) => b.price - a.price)
      .slice(0, 10);

    // Format and sort asks (price ascending)
    const aggregatedAsks = Object.keys(askMap)
      .map(priceStr => ({
        price: Number(priceStr),
        amount: askMap[Number(priceStr)]
      }))
      .sort((a, b) => a.price - b.price)
      .slice(0, 10);

    return {
      bids: aggregatedBids,
      asks: aggregatedAsks
    };
  }

  /**
   * Utility to find an order by its ID.
   */
  public getOrderById(orderId: string): Order | undefined {
    for (const orderList of this.orders.values()) {
      const order = orderList.find(o => o.id === orderId);
      if (order) return order;
    }
    return undefined;
  }
}
