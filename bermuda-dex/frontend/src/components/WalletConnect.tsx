import React, { useState } from 'react'
import { useAccount, useConnect, useDisconnect, useBalance } from 'wagmi'
import { Wallet, LogOut, Copy, Check, ChevronDown } from 'lucide-react'

export const WalletConnect: React.FC = () => {
  const { address, isConnected, connector } = useAccount()
  const { connect, connectors } = useConnect()
  const { disconnect } = useDisconnect()
  const { data: balance } = useBalance({ address })

  const [copied, setCopied] = useState(false)
  const [dropdownOpen, setDropdownOpen] = useState(false)

  const copyAddress = () => {
    if (address) {
      navigator.clipboard.writeText(address)
      setCopied(true)
      setTimeout(() => setCopied(false), 2000)
    }
  }

  const truncateAddress = (addr: string) => {
    return `${addr.slice(0, 6)}...${addr.slice(-4)}`
  }

  return (
    <div className="relative inline-block text-left">
      {isConnected && address ? (
        <div className="flex items-center gap-2">
          {/* Balance display */}
          <div className="hidden sm:flex flex-col items-end px-3 py-1 bg-[#161920] border border-borderDark rounded-md">
            <span className="text-[10px] text-gray-500 uppercase tracking-wider font-semibold">Balance</span>
            <span className="text-sm font-medium text-gray-200">
              {balance ? `${parseFloat(balance.formatted).toFixed(4)} ${balance.symbol}` : '0.00 ETH'}
            </span>
          </div>

          {/* Connected Button */}
          <button
            onClick={() => setDropdownOpen(!dropdownOpen)}
            className="flex items-center gap-2 bg-[#1a1d24] border border-borderDark hover:border-baseBlue text-gray-200 px-4 py-2 rounded-md transition duration-150 text-sm font-medium"
          >
            <div className="w-2 h-2 rounded-full bg-emerald-500 animate-pulse" />
            <span>{truncateAddress(address)}</span>
            <ChevronDown className="w-4 h-4 text-gray-500" />
          </button>

          {/* Dropdown Menu */}
          {dropdownOpen && (
            <>
              <div 
                className="fixed inset-0 z-10" 
                onClick={() => setDropdownOpen(false)}
              />
              <div className="absolute right-0 mt-2 w-56 rounded-md bg-[#1a1d24] border border-borderDark shadow-2xl z-20 overflow-hidden">
                <div className="px-4 py-3 border-b border-borderDark bg-[#161920]">
                  <p className="text-xs text-gray-500 font-medium uppercase tracking-wider">Connected Wallet</p>
                  <p className="text-xs font-mono text-gray-300 truncate mt-1">{address}</p>
                  <p className="text-xs text-gray-400 mt-2 bg-darkBg/50 px-2 py-1 rounded inline-block">
                    {connector?.name || 'Injected'}
                  </p>
                </div>
                <div className="py-1">
                  <button
                    onClick={copyAddress}
                    className="flex w-full items-center gap-2 px-4 py-2.5 text-sm text-gray-300 hover:bg-[#232731] transition duration-150 text-left"
                  >
                    {copied ? (
                      <>
                        <Check className="w-4 h-4 text-emerald-500" />
                        <span className="text-emerald-500">Copied!</span>
                      </>
                    ) : (
                      <>
                        <Copy className="w-4 h-4 text-gray-400" />
                        <span>Copy Address</span>
                      </>
                    )}
                  </button>
                  <button
                    onClick={() => {
                      disconnect()
                      setDropdownOpen(false)
                    }}
                    className="flex w-full items-center gap-2 px-4 py-2.5 text-sm text-rose-400 hover:bg-rose-500/10 transition duration-150 text-left"
                  >
                    <LogOut className="w-4 h-4" />
                    <span>Disconnect</span>
                  </button>
                </div>
              </div>
            </>
          )}
        </div>
      ) : (
        <div className="flex gap-2">
          {connectors.map((conn) => (
            <button
              key={conn.id}
              onClick={() => connect({ connector: conn })}
              className="flex items-center gap-2 bg-[#1a1d24] border border-borderDark hover:border-baseBlue hover:bg-[#232731] text-gray-200 px-4 py-2 rounded-md transition duration-150 text-sm font-medium"
            >
              <Wallet className="w-4 h-4 text-baseBlue" />
              <span>Connect {conn.name}</span>
            </button>
          ))}
        </div>
      )}
    </div>
  )
}
