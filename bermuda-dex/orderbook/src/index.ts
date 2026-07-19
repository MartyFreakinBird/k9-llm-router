import express from 'express';
import { createServer } from 'http';
import { WebSocketServer, WebSocket } from 'ws';
import * as dotenv from 'dotenv';
import { CLOBEngine } from './clob';
import { CLOBSigner } from './signer';
import { Order, OrderSide } from './models';
import { ChainlinkOracle } from './oracle';

dotenv.config();

const app = express();
const port = process.env.PORT || 3001;

// Initialize middlewares
app.use(express.json());

// Initialize core components
const clob = new CLOBEngine();
const signer = new CLOBSigner();
const oracle = new ChainlinkOracle();

// Create HTTP server
const server = createServer(app);

// Create WebSocket server attached to the HTTP server
const wss = new WebSocketServer({ server });

// Set of connected WS clients
const activeWsClients = new Set<WebSocket>();

wss.on('connection', (ws: WebSocket) => {
  console.log('WS Client connected');
  activeWsClients.add(ws);

  ws.on('close', () => {
    console.log('WS Client disconnected');
    activeWsClients.delete(ws);
  });

  ws.on('error', (err) => {
    console.error('WS error:', err);
  });
});

/**
 * Broadcasts the updated L2 order book to all connected WS clients.
 */
function broadcastOrderBook(pair: string) {
  const orderbook = clob.getOrderBook(pair);
  const payload = JSON.stringify({
    event: 'orderbook_update',
    pair,
    timestamp: Date.now(),
    data: orderbook
  });

  for (const client of activeWsClients) {
    if (client.readyState === WebSocket.OPEN) {
      client.send(payload);
    }
  }
}

/**
 * Broadcasts the newly matched trades to all connected WS clients.
 */
function broadcastTrades(pair: string, trades: any[]) {
  const payload = JSON.stringify({
    event: 'trades_update',
    pair,
    timestamp: Date.now(),
    data: trades
  });

  for (const client of activeWsClients) {
    if (client.readyState === WebSocket.OPEN) {
      client.send(payload);
    }
  }
}

// REST Endpoints

/**
 * GET /health - Check server health
 */
app.get('/health', (req, res) => {
  res.json({
    status: 'healthy',
    timestamp: Date.now(),
    engine: 'Bermuda RWA CLOB Engine',
    uptime: process.uptime()
  });
});

/**
 * GET /orderbook/:pair - Get top 10 bids/asks for a pair
 * Decodes the pair parameter correctly (e.g., zKRW/zGOLD or zKRW%2FzGOLD)
 */
app.get('/orderbook/:pair(*)', (req, res) => {
  const pair = req.params.pair;
  if (!pair) {
    res.status(400).json({ error: 'Pair parameter is required' });
    return;
  }
  const orderbook = clob.getOrderBook(pair);
  res.json(orderbook);
});

/**
 * GET /trades/:pair - Get last 50 trades for a pair
 */
app.get('/trades/:pair(*)', (req, res) => {
  const pair = req.params.pair;
  if (!pair) {
    res.status(400).json({ error: 'Pair parameter is required' });
    return;
  }
  const allTrades = clob.getTrades(pair);
  // Return last 50 trades in descending order of execution (most recent first)
  const last50 = allTrades.slice(-50).reverse();
  res.json(last50);
});

/**
 * POST /orders - Place a limit order
 */
app.post('/orders', async (req, res) => {
  const { pair, side, price, amount, walletAddress } = req.body;

  // Input validations
  if (!pair || typeof pair !== 'string') {
    res.status(400).json({ error: 'Invalid or missing pair (e.g. zKRW/zGOLD)' });
    return;
  }
  if (side !== 'BUY' && side !== 'SELL') {
    res.status(400).json({ error: "Side must be either 'BUY' or 'SELL'" });
    return;
  }
  if (typeof price !== 'number' || price <= 0) {
    res.status(400).json({ error: 'Price must be a number greater than 0' });
    return;
  }
  if (typeof amount !== 'number' || amount <= 0) {
    res.status(400).json({ error: 'Amount must be a number greater than 0' });
    return;
  }
  if (!walletAddress || typeof walletAddress !== 'string') {
    res.status(400).json({ error: 'walletAddress must be a valid string address' });
    return;
  }

  // Create order
  const orderId = 'order_' + Math.random().toString(36).substr(2, 9);
  const newOrder: Order = {
    id: orderId,
    pair,
    side: side as OrderSide,
    price,
    amount,
    filledAmount: 0,
    walletAddress,
    status: 'OPEN',
    timestamp: Date.now()
  };

  try {
    // Add to CLOB Engine
    clob.addOrder(newOrder);

    // Broadcast L2 orderbook right after order placement
    broadcastOrderBook(pair);

    // Run matching engine
    const matchedTrades = clob.match(pair);

    // If matches occurred, process atomic swaps asynchronously
    if (matchedTrades.length > 0) {
      // Broadcast updated L2 book since some orders got filled/partially filled
      broadcastOrderBook(pair);
      
      // Process on-chain relayer/swaps for each trade
      for (const trade of matchedTrades) {
        const buyOrder = clob.getOrderById(trade.buyOrderId);
        const sellOrder = clob.getOrderById(trade.sellOrderId);

        let initiatorAddress = '';
        let recipientAddress = '';

        if (buyOrder && sellOrder) {
          // Taker is initiator (the order with the larger timestamp)
          if (buyOrder.timestamp > sellOrder.timestamp) {
            initiatorAddress = buyOrder.walletAddress;
            recipientAddress = sellOrder.walletAddress;
          } else {
            initiatorAddress = sellOrder.walletAddress;
            recipientAddress = buyOrder.walletAddress;
          }
        } else {
          initiatorAddress = buyOrder?.walletAddress || '0x0000000000000000000000000000000000000000';
          recipientAddress = sellOrder?.walletAddress || '0x0000000000000000000000000000000000000000';
        }

        // Run the trade signer & HTLC contract swap initiation asynchronously
        signer.processTradeAndOpenSwap(trade, initiatorAddress, recipientAddress)
          .then(result => {
            console.log(`Successfully processed swap for Trade ${trade.id}:`, result);
            // Broadcast trades again with the updated hashlock and secret
            broadcastTrades(pair, matchedTrades);
          })
          .catch(err => {
            console.error(`Failed to process swap for Trade ${trade.id}:`, err);
          });
      }

      broadcastTrades(pair, matchedTrades);
    }

    res.status(201).json({
      success: true,
      message: 'Order placed successfully',
      order: newOrder,
      trades: matchedTrades
    });
  } catch (error: any) {
    console.error('Error handling order placement:', error);
    res.status(500).json({ error: 'Internal server error while placing order' });
  }
});

/**
 * DELETE /orders/:id - Cancel an order
 */
app.delete('/orders/:id', (req, res) => {
  const orderId = req.params.id;
  if (!orderId) {
    res.status(400).json({ error: 'Order ID is required' });
    return;
  }

  // Find the order before cancelling to know its pair (for broadcasting)
  const order = clob.getOrderById(orderId);
  if (!order) {
    res.status(404).json({ error: 'Order not found' });
    return;
  }

  const cancelled = clob.cancelOrder(orderId);
  if (cancelled) {
    broadcastOrderBook(order.pair);
    res.json({
      success: true,
      message: `Order ${orderId} cancelled successfully`
    });
  } else {
    res.status(400).json({
      error: `Order ${orderId} could not be cancelled (already filled or cancelled)`
    });
  }
});

/**
 * GET /oracle/price/:pair - Bonus endpoint: Fetches oracle price from Chainlink Price Feed
 */
app.get('/oracle/price/:pair(*)', async (req, res) => {
  const pair = req.params.pair;
  if (!pair) {
    res.status(400).json({ error: 'Pair parameter is required' });
    return;
  }
  try {
    const price = await oracle.getLatestPrice(pair);
    res.json({ pair, price, source: 'Chainlink AggregatorV3 on Base Sepolia' });
  } catch (err: any) {
    res.status(500).json({ error: err.message });
  }
});

// Start listening
server.listen(port, () => {
  console.log(`====================================================`);
  console.log(`Bermuda RWA DEX CLOB Matching Engine running at:`);
  console.log(`HTTP/WS Server: http://localhost:${port}`);
  console.log(`Environment: ${process.env.NODE_ENV || 'development'}`);
  console.log(`====================================================`);
});
