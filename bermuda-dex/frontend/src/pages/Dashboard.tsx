import React, { useState } from 'react'
import { WalletConnect } from '../components/WalletConnect'
import { KYCStatus } from '../components/KYCStatus'
import { SwapPanel } from '../components/SwapPanel'
import { OrderBook } from '../components/OrderBook'
import { TradeHistory, Trade } from '../components/TradeHistory'
import { ShieldCheck, Anchor, Globe, TrendingUp, DollarSign, Award, ChevronRight, HelpCircle } from 'lucide-react'

export const Dashboard: React.FC = () => {
  const [hasKyc, setHasKyc] = useState<boolean>(false)
  const [newTrade, setNewTrade] = useState<Trade | null>(null)

  const handleKycChange = (status: boolean) => {
    setHasKyc(status)
  }

  const handleTradeExecuted = (tradeData: {
    pair: string
    side: 'BUY' | 'SELL'
    price: number
    amount: number
    txHash: string
  }) => {
    const trade: Trade = {
      id: `trade-user-${Date.now()}`,
      pair: tradeData.pair,
      side: tradeData.side,
      price: tradeData.price,
      amount: tradeData.amount,
      timestamp: new Date(),
      txHash: tradeData.txHash,
    }
    setNewTrade(trade)
  }

  const triggerKycFlow = () => {
    // This allows children to trigger KYC flow easily
    const completeButton = document.querySelector('button[class*="bg-rose-500"]') as HTMLButtonElement
    if (completeButton) {
      completeButton.click()
    }
  }

  return (
    <div className="min-h-screen bg-darkBg text-gray-100 flex flex-col">
      {/* Institutional Top Ticker Bar */}
      <div className="bg-[#11131a] border-b border-borderDark text-[11px] text-gray-400 py-2 px-4 flex justify-between items-center overflow-x-auto whitespace-nowrap">
        <div className="flex items-center gap-5">
          <span className="flex items-center gap-1">
            <Anchor className="w-3.5 h-3.5 text-baseBlue" />
            <span className="font-semibold text-gray-300">Bermuda Monetary Authority (BMA) License:</span>
            <span className="font-mono text-emerald-400">#RWA-DEX-2026-09</span>
          </span>
          <span className="flex items-center gap-1.5 border-l border-borderDark pl-5">
            <span className="text-gray-500 uppercase font-semibold">zKRW Mid:</span>
            <span className="font-mono text-gray-200">1,348.50 KRW</span>
          </span>
          <span className="flex items-center gap-1.5 border-l border-borderDark pl-5">
            <span className="text-gray-500 uppercase font-semibold">zGOLD Mid:</span>
            <span className="font-mono text-gray-200">76.85 USD/g</span>
          </span>
          <span className="flex items-center gap-1.5 border-l border-borderDark pl-5">
            <span className="text-gray-500 uppercase font-semibold">Escrow TVL:</span>
            <span className="font-mono text-emerald-400">$184.5M USD</span>
          </span>
        </div>
        <div className="flex items-center gap-4 border-l border-borderDark pl-5">
          <span className="flex items-center gap-1">
            <span className="w-1.5 h-1.5 rounded-full bg-emerald-400 animate-ping" />
            <span className="text-gray-300 font-semibold font-mono">Base Network Load: Normal</span>
          </span>
        </div>
      </div>

      {/* Header */}
      <header className="bg-cardBg border-b border-borderDark py-4 px-6 flex flex-col md:flex-row justify-between items-center gap-4">
        <div className="flex items-center gap-3">
          <div className="bg-baseBlue p-2 rounded-lg text-white flex items-center justify-center shadow-lg shadow-baseBlue/20">
            <Anchor className="w-6 h-6" />
          </div>
          <div>
            <div className="flex items-center gap-2">
              <h1 className="text-xl font-bold tracking-tight text-white font-sans">BERMUDA PACIFIC</h1>
              <span className="bg-baseBlue/15 text-baseBlue text-[9px] font-extrabold px-1.5 py-0.5 rounded font-mono uppercase tracking-wider">
                HNWI Desk
              </span>
            </div>
            <p className="text-xs text-gray-400">Atlanta Accredited Investor RWA Swap Gateway</p>
          </div>
        </div>

        {/* Global Stats */}
        <div className="hidden lg:flex items-center gap-6 text-xs border-x border-borderDark px-6 py-1">
          <div>
            <span className="text-gray-500 uppercase font-bold text-[10px]">Sales Desk Jurisdiction</span>
            <p className="text-gray-200 font-semibold mt-0.5">SEC Reg D / Reg S (Atlanta)</p>
          </div>
          <div>
            <span className="text-gray-500 uppercase font-bold text-[10px]">Atomic Clearing Model</span>
            <p className="text-gray-200 font-semibold mt-0.5">CLOB-AMM Hybrid Base L2</p>
          </div>
          <div>
            <span className="text-gray-500 uppercase font-bold text-[10px]">Audit Cycle</span>
            <p className="text-gray-200 font-semibold mt-0.5">Real-time Proof of Reserves</p>
          </div>
        </div>

        {/* Wallet Connect & Portal Access */}
        <div className="flex items-center gap-3">
          <WalletConnect />
        </div>
      </header>

      {/* Dashboard Body Grid */}
      <main className="flex-1 p-6 grid grid-cols-1 lg:grid-cols-12 gap-6 max-w-[1600px] w-full mx-auto">
        {/* Left Side: Orderbook and History (col-span 7) */}
        <div className="lg:col-span-7 space-y-6 flex flex-col">
          {/* L2 Orderbook */}
          <OrderBook />

          {/* Trade History */}
          <TradeHistory newTrade={newTrade} />
        </div>

        {/* Right Side: Swap Panel, KYC status, Specs (col-span 5) */}
        <div className="lg:col-span-5 space-y-6">
          {/* KYC Passport Status */}
          <KYCStatus kycOverride={hasKyc} onKycChange={handleKycChange} />

          {/* Swap Panel */}
          <SwapPanel
            hasKyc={hasKyc}
            triggerKycFlow={triggerKycFlow}
            onTradeExecuted={handleTradeExecuted}
          />

          {/* Asset Specifications & Regulatory Blueprint Card */}
          <div className="bg-cardBg border border-borderDark rounded-lg p-5 space-y-5">
            <h3 className="text-sm font-semibold uppercase tracking-wider text-gray-400 pb-2 border-b border-borderDark flex items-center gap-2">
              <ShieldCheck className="w-4 h-4 text-baseBlue" />
              <span>Accredited Asset Directory</span>
            </h3>

            {/* Token Spec 1 */}
            <div className="space-y-2">
              <div className="flex justify-between items-center">
                <div className="flex items-center gap-2">
                  <div className="w-2.5 h-2.5 rounded-full bg-blue-400" />
                  <span className="text-sm font-semibold text-gray-200">zKRW (Korean Equity Index)</span>
                </div>
                <span className="text-[10px] font-mono bg-blue-500/10 text-blue-400 px-1.5 py-0.5 rounded font-bold">EQUITY</span>
              </div>
              <p className="text-xs text-gray-400 leading-relaxed">
                Atomic token pegged to the basket of top Korean Equities (Samsung, SK Hynix, Hyundai). Fully backed by physical equities held in custody by Hana Bank, Seoul.
              </p>
              <div className="grid grid-cols-2 gap-2 text-[10px] font-mono text-gray-500 bg-[#161920] p-2 rounded">
                <div>Ticker: <span className="text-gray-300">zKRW</span></div>
                <div>Standard: <span className="text-gray-300">ERC-20 + BMA</span></div>
                <div>Mint Cap: <span className="text-gray-300">50,000,000 zKRW</span></div>
                <div>Custodian: <span className="text-gray-300">Hana Bank Korea</span></div>
              </div>
            </div>

            {/* Token Spec 2 */}
            <div className="space-y-2 pt-2 border-t border-borderDark/50">
              <div className="flex justify-between items-center">
                <div className="flex items-center gap-2">
                  <div className="w-2.5 h-2.5 rounded-full bg-amber-400" />
                  <span className="text-sm font-semibold text-gray-200">zGOLD (Nigerian Gold Commodity)</span>
                </div>
                <span className="text-[10px] font-mono bg-amber-500/10 text-amber-400 px-1.5 py-0.5 rounded font-bold">COMMODITY</span>
              </div>
              <p className="text-xs text-gray-400 leading-relaxed">
                Direct-backed token representing 1 gram of physical, certified fine gold bullion (99.9% purity) stored in secure vault facilities at Access Bank PLC, Lagos.
              </p>
              <div className="grid grid-cols-2 gap-2 text-[10px] font-mono text-gray-500 bg-[#161920] p-2 rounded">
                <div>Ticker: <span className="text-gray-300">zGOLD</span></div>
                <div>Standard: <span className="text-gray-300">ERC-20 + BMA</span></div>
                <div>Mint Cap: <span className="text-gray-300">200,000 zGOLD</span></div>
                <div>Custodian: <span className="text-gray-300">Access Bank Nigeria</span></div>
              </div>
            </div>

            {/* Regulatory Blueprint */}
            <div className="space-y-2 pt-2 border-t border-borderDark/50">
              <span className="text-xs font-semibold text-gray-400 flex items-center gap-1.5">
                <Globe className="w-3.5 h-3.5 text-baseBlue" />
                <span>Inter-jurisdictional Architecture</span>
              </span>
              <table className="w-full text-[10px] font-mono text-left text-gray-400 mt-1">
                <thead>
                  <tr className="border-b border-borderDark/40">
                    <th className="py-1">Jurisdiction</th>
                    <th className="py-1">Regulator</th>
                    <th className="py-1">Function</th>
                  </tr>
                </thead>
                <tbody>
                  <tr>
                    <td className="py-1 text-gray-300">Bermuda</td>
                    <td className="py-1">BMA (DABA-2022)</td>
                    <td className="py-1">DEX & Smart Contract Escrow</td>
                  </tr>
                  <tr>
                    <td className="py-1 text-gray-300">Atlanta, USA</td>
                    <td className="py-1">SEC (Reg D 506c)</td>
                    <td className="py-1">Qualified HNWI Offering Desk</td>
                  </tr>
                  <tr>
                    <td className="py-1 text-gray-300">Seoul, Korea</td>
                    <td className="py-1">FSC (Korea)</td>
                    <td className="py-1">zKRW Underlying Equities Custody</td>
                  </tr>
                  <tr>
                    <td className="py-1 text-gray-300">Lagos, Nigeria</td>
                    <td className="py-1">SEC (Nigeria)</td>
                    <td className="py-1">zGOLD Bullion Vault Custody</td>
                  </tr>
                </tbody>
              </table>
            </div>
          </div>
        </div>
      </main>

      {/* Footer */}
      <footer className="bg-cardBg border-t border-borderDark py-6 px-8 mt-12 text-xs text-gray-500 flex flex-col md:flex-row justify-between items-center gap-4">
        <div>
          <p>© 2026 Bermuda Pacific Ltd. All rights reserved.</p>
          <p className="mt-1">
            Trading real-world assets is subject to strict regulatory compliance, accredited investor checks, and localized tax withholding.
          </p>
        </div>
        <div className="flex gap-4 font-semibold text-gray-400">
          <a href="#compliance" className="hover:text-baseBlue transition duration-150">BMA Disclosures</a>
          <span>•</span>
          <a href="#custody" className="hover:text-baseBlue transition duration-150">Custodial Audits</a>
          <span>•</span>
          <a href="#terms" className="hover:text-baseBlue transition duration-150">Private Offering Memorandum</a>
        </div>
      </footer>
    </div>
  )
}
