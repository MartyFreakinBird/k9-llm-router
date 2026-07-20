import React, { useState, useEffect } from 'react'
import { ExternalLink, Hourglass } from 'lucide-react'

export interface Trade {
  id: string
  pair: string
  side: 'BUY' | 'SELL'
  price: number
  amount: number
  timestamp: Date
  txHash: string
}

interface TradeHistoryProps {
  newTrade: Trade | null
}

export const TradeHistory: React.FC<TradeHistoryProps> = ({ newTrade }) => {
  const [trades, setTrades] = useState<Trade[]>([])

  // Seed initial trades on component mount
  useEffect(() => {
    const seedTrades: Trade[] = []
    const now = new Date()
    const basePrice = 0.0782

    for (let i = 0; i < 8; i++) {
      const side = Math.random() > 0.5 ? ('BUY' as const) : ('SELL' as const)
      const priceOffset = (Math.random() - 0.5) * 0.001
      const price = parseFloat((basePrice + priceOffset).toFixed(4))
      const amount = parseFloat((Math.random() * 5000 + 500).toFixed(2))
      const timeOffset = (i + 1) * (Math.random() * 5 + 1) * 60 * 1000 // minutes ago
      const mockHash = '0x' + Array.from({ length: 64 }, () => Math.floor(Math.random() * 16).toString(16)).join('')

      seedTrades.push({
        id: `trade-seed-${i}`,
        pair: 'zGOLD / zKRW',
        side,
        price,
        amount,
        timestamp: new Date(now.getTime() - timeOffset),
        txHash: mockHash,
      })
    }

    setTrades(seedTrades)
  }, [])

  // Listen for new trades from user interaction
  useEffect(() => {
    if (newTrade) {
      setTrades((prevTrades) => {
        const updated = [newTrade, ...prevTrades]
        // Cap at 10 items
        return updated.slice(0, 10)
      })
    }
  }, [newTrade])

  const formatTxHash = (hash: string) => {
    return `${hash.slice(0, 6)}...${hash.slice(-4)}`
  }

  const formatTime = (date: Date) => {
    return date.toLocaleTimeString(undefined, {
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
      hour12: false,
    })
  }

  return (
    <div className="bg-cardBg border border-borderDark rounded-lg p-5">
      <div className="flex justify-between items-center mb-4 pb-2 border-b border-borderDark">
        <div className="flex items-center gap-2">
          <h3 className="text-sm font-semibold uppercase tracking-wider text-gray-400">Public Trade Executions</h3>
          <span className="bg-baseBlue/10 text-baseBlue text-[10px] px-2 py-0.5 rounded font-mono font-bold uppercase tracking-wide animate-pulse">
            LIVE FEED
          </span>
        </div>
      </div>

      <div className="overflow-x-auto">
        <table className="w-full text-left text-xs font-mono">
          <thead>
            <tr className="text-gray-500 uppercase text-[10px] border-b border-borderDark/40 pb-2">
              <th className="py-2.5 font-bold">Time</th>
              <th className="py-2.5 font-bold">Pair</th>
              <th className="py-2.5 font-bold text-center">Side</th>
              <th className="py-2.5 text-right font-bold">Price (zKRW)</th>
              <th className="py-2.5 text-right font-bold">Amount (zGOLD)</th>
              <th className="py-2.5 text-right font-bold">Transaction</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-borderDark/20">
            {trades.length === 0 ? (
              <tr>
                <td colSpan={6} className="text-center py-8 text-gray-500">
                  <Hourglass className="w-5 h-5 mx-auto animate-spin mb-2" />
                  <span>Loading network RWA trades...</span>
                </td>
              </tr>
            ) : (
              trades.map((trade) => (
                <tr key={trade.id} className="hover:bg-[#1e222b]/45 transition duration-150">
                  <td className="py-2.5 text-gray-400">{formatTime(trade.timestamp)}</td>
                  <td className="py-2.5 text-gray-300 font-semibold">{trade.pair}</td>
                  <td className="py-2.5 text-center">
                    <span
                      className={`inline-block px-1.5 py-0.5 rounded text-[10px] font-bold ${
                        trade.side === 'BUY'
                          ? 'bg-emerald-500/10 text-emerald-400 border border-emerald-500/10'
                          : 'bg-rose-500/10 text-rose-400 border border-rose-500/10'
                      }`}
                    >
                      {trade.side}
                    </span>
                  </td>
                  <td className={`py-2.5 text-right font-semibold ${
                    trade.side === 'BUY' ? 'text-emerald-400' : 'text-rose-400'
                  }`}>
                    {trade.price.toFixed(4)}
                  </td>
                  <td className="py-2.5 text-right text-gray-200 font-semibold">
                    {trade.amount.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
                  </td>
                  <td className="py-2.5 text-right">
                    <a
                      href={`https://sepolia.basescan.org/tx/${trade.txHash}`}
                      target="_blank"
                      rel="noreferrer"
                      className="inline-flex items-center gap-1 text-baseBlue hover:text-blue-400 hover:underline transition duration-150"
                    >
                      <span>{formatTxHash(trade.txHash)}</span>
                      <ExternalLink className="w-3 h-3" />
                    </a>
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
    </div>
  )
}
