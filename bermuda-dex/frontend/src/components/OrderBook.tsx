import React, { useState, useEffect, useRef } from 'react'
import { Activity, Radio, RadioOff } from 'lucide-react'

interface OrderBookItem {
  price: number
  amount: number
  total: number
}

export const OrderBook: React.FC = () => {
  const [bids, setBids] = useState<OrderBookItem[]>([])
  const [asks, setAsks] = useState<OrderBookItem[]>([])
  const [wsStatus, setWsStatus] = useState<'CONNECTING' | 'CONNECTED' | 'DISCONNECTED'>('DISCONNECTED')
  const [lastPrice, setLastPrice] = useState<number>(0.0782)
  const [priceChange, setPriceChange] = useState<'UP' | 'DOWN' | 'NONE'>('NONE')

  const wsRef = useRef<WebSocket | null>(null)

  // Fallback / simulation interval ref
  const simulationInterval = useRef<NodeJS.Timeout | null>(null)

  // Generate initial mock order book data
  const generateInitialData = () => {
    const basePrice = 0.0782
    const initialBids: OrderBookItem[] = []
    const initialAsks: OrderBookItem[] = []

    for (let i = 1; i <= 8; i++) {
      const bidPrice = basePrice - i * 0.0001
      const bidAmount = Math.random() * 15000 + 1000
      initialBids.push({
        price: parseFloat(bidPrice.toFixed(4)),
        amount: parseFloat(bidAmount.toFixed(2)),
        total: parseFloat((bidPrice * bidAmount).toFixed(2)),
      })

      const askPrice = basePrice + i * 0.0001
      const askAmount = Math.random() * 15000 + 1000
      initialAsks.push({
        price: parseFloat(askPrice.toFixed(4)),
        amount: parseFloat(askAmount.toFixed(2)),
        total: parseFloat((askPrice * askAmount).toFixed(2)),
      })
    }

    setBids(initialBids.sort((a, b) => b.price - a.price))
    setAsks(initialAsks.sort((a, b) => b.price - a.price))
  }

  // Setup WebSocket connection
  useEffect(() => {
    generateInitialData()
    setWsStatus('CONNECTING')

    // Connect to WebSocket port 3001
    const connectWs = () => {
      const wsUrl = 'ws://localhost:3001'
      const ws = new WebSocket(wsUrl)
      wsRef.current = ws

      ws.onopen = () => {
        setWsStatus('CONNECTED')
        // Stop mock simulation if WS connects
        if (simulationInterval.current) {
          clearInterval(simulationInterval.current)
          simulationInterval.current = null
        }
      }

      ws.onmessage = (event) => {
        try {
          const data = JSON.parse(event.data)
          if (data.bids) setBids(data.bids)
          if (data.asks) setAsks(data.asks)
          if (data.lastPrice) {
            setPriceChange(data.lastPrice > lastPrice ? 'UP' : data.lastPrice < lastPrice ? 'DOWN' : 'NONE')
            setLastPrice(data.lastPrice)
          }
        } catch (e) {
          console.error('Failed to parse WS L2 Orderbook message', e)
        }
      }

      ws.onerror = (err) => {
        console.warn('WebSocket L2 server connection refused. Defaulting to high-accuracy sandbox simulator.', err)
        setWsStatus('DISCONNECTED')
      }

      ws.onclose = () => {
        setWsStatus('DISCONNECTED')
        // Restart mock simulation if closed
        startSimulation()
      }
    }

    // High fidelity simulator when WS server is offline (default sandbox behavior)
    const startSimulation = () => {
      if (simulationInterval.current) return

      simulationInterval.current = setInterval(() => {
        // Randomly update bids and asks slightly
        setBids((prevBids) => {
          const updated = prevBids.map((bid) => {
            if (Math.random() > 0.6) {
              const delta = (Math.random() - 0.5) * 500
              const newAmount = Math.max(100, bid.amount + delta)
              return {
                ...bid,
                amount: parseFloat(newAmount.toFixed(2)),
                total: parseFloat((bid.price * newAmount).toFixed(2)),
              }
            }
            return bid
          })
          return [...updated]
        })

        setAsks((prevAsks) => {
          const updated = prevAsks.map((ask) => {
            if (Math.random() > 0.6) {
              const delta = (Math.random() - 0.5) * 500
              const newAmount = Math.max(100, ask.amount + delta)
              return {
                ...ask,
                amount: parseFloat(newAmount.toFixed(2)),
                total: parseFloat((ask.price * newAmount).toFixed(2)),
              }
            }
            return ask
          })
          return [...updated]
        })

        // Fluctuate price slightly
        if (Math.random() > 0.8) {
          const priceDelta = (Math.random() - 0.5) * 0.0003
          setLastPrice((prev) => {
            const nextPrice = Math.max(0.01, prev + priceDelta)
            setPriceChange(nextPrice > prev ? 'UP' : 'DOWN')
            return parseFloat(nextPrice.toFixed(4))
          })
        }
      }, 1000)
    }

    // Initiate
    connectWs()
    startSimulation()

    return () => {
      if (wsRef.current) {
        wsRef.current.close()
      }
      if (simulationInterval.current) {
        clearInterval(simulationInterval.current)
      }
    }
  }, [])

  // Spread calculation
  const topBid = bids[0]?.price || 0
  const topAsk = asks[asks.length - 1]?.price || 0
  const spread = Math.max(0, topAsk - topBid)
  const spreadPercent = topAsk > 0 ? (spread / topAsk) * 100 : 0

  return (
    <div className="bg-cardBg border border-borderDark rounded-lg p-5 flex flex-col h-[540px]">
      {/* Title + Connection Badge */}
      <div className="flex justify-between items-center mb-4 pb-2 border-b border-borderDark">
        <div className="flex items-center gap-2">
          <h3 className="text-sm font-semibold uppercase tracking-wider text-gray-400">Order Book L2</h3>
          <span className="bg-[#1e222b] text-[10px] text-gray-400 px-1.5 py-0.5 rounded font-mono font-medium">
            zGOLD / zKRW
          </span>
        </div>

        <div className="flex items-center gap-1.5">
          {wsStatus === 'CONNECTED' ? (
            <div className="flex items-center gap-1 text-[10px] text-emerald-400 font-semibold uppercase tracking-wider">
              <Radio className="w-3.5 h-3.5 animate-pulse" />
              <span>LIVE WS :3001</span>
            </div>
          ) : (
            <div className="flex items-center gap-1 text-[10px] text-amber-500 font-semibold uppercase tracking-wider" title="Using internal low-latency broker fallback simulator">
              <Activity className="w-3.5 h-3.5" />
              <span>SIMULATOR</span>
            </div>
          )}
        </div>
      </div>

      {/* Header columns */}
      <div className="grid grid-cols-3 text-right text-[10px] text-gray-500 uppercase font-bold tracking-wider mb-2">
        <div className="text-left">Price (zKRW)</div>
        <div>Size (zGOLD)</div>
        <div>Total (zKRW)</div>
      </div>

      {/* ASKS (Sells) - Rendered from bottom to top (highest to lowest/closest to spread) */}
      <div className="flex-1 flex flex-col justify-end overflow-hidden space-y-[2px] mb-2 font-mono text-xs">
        {asks.slice().reverse().map((ask, idx) => (
          <div key={`ask-${idx}`} className="grid grid-cols-3 text-right text-rose-400 hover:bg-rose-500/5 py-0.5 px-1 rounded transition duration-100 relative">
            {/* Visual depth bar */}
            <div 
              className="absolute right-0 top-0 bottom-0 bg-rose-500/10 z-0 transition-all duration-300 pointer-events-none"
              style={{ width: `${Math.min(100, (ask.amount / 20000) * 100)}%` }}
            />
            <div className="text-left font-bold z-10">{ask.price.toFixed(4)}</div>
            <div className="text-gray-300 z-10">{ask.amount.toLocaleString(undefined, { maximumFractionDigits: 1 })}</div>
            <div className="text-gray-400 z-10">{ask.total.toLocaleString(undefined, { maximumFractionDigits: 0 })}</div>
          </div>
        ))}
      </div>

      {/* SPREAD/MID-PRICE */}
      <div className="bg-[#161920] border-y border-borderDark py-2.5 px-2 my-2 flex justify-between items-center text-sm font-semibold">
        <div className="flex items-center gap-2">
          <span className={`text-base font-bold font-mono transition-colors duration-300 ${
            priceChange === 'UP' ? 'text-emerald-400' : priceChange === 'DOWN' ? 'text-rose-400' : 'text-gray-200'
          }`}>
            {lastPrice.toFixed(4)}
          </span>
          <span className="text-[10px] text-gray-500 font-mono uppercase font-bold">
            Mid Price
          </span>
        </div>
        <div className="text-[11px] text-gray-400 font-mono">
          Spread: <span className="text-gray-300">{spread.toFixed(4)}</span> ({spreadPercent.toFixed(2)}%)
        </div>
      </div>

      {/* BIDS (Buys) */}
      <div className="flex-1 overflow-hidden space-y-[2px] font-mono text-xs">
        {bids.map((bid, idx) => (
          <div key={`bid-${idx}`} className="grid grid-cols-3 text-right text-emerald-400 hover:bg-emerald-500/5 py-0.5 px-1 rounded transition duration-100 relative">
            {/* Visual depth bar */}
            <div 
              className="absolute right-0 top-0 bottom-0 bg-emerald-500/10 z-0 transition-all duration-300 pointer-events-none"
              style={{ width: `${Math.min(100, (bid.amount / 20000) * 100)}%` }}
            />
            <div className="text-left font-bold z-10">{bid.price.toFixed(4)}</div>
            <div className="text-gray-300 z-10">{bid.amount.toLocaleString(undefined, { maximumFractionDigits: 1 })}</div>
            <div className="text-gray-400 z-10">{bid.total.toLocaleString(undefined, { maximumFractionDigits: 0 })}</div>
          </div>
        ))}
      </div>
    </div>
  )
}
