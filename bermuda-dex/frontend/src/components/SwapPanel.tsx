import React, { useState, useEffect } from 'react'
import { useAccount, useWriteContract, useWaitForTransactionReceipt } from 'wagmi'
import { parseUnits, keccak256, stringToBytes, toHex } from 'viem'
import { AlertCircle, ArrowDownUp, CheckCircle, HelpCircle, Loader2 } from 'lucide-react'
import { RWA_SWAP_ABI, CONTRACT_ADDRESSES } from '../config/contracts'

interface SwapPanelProps {
  hasKyc: boolean
  triggerKycFlow: () => void
  onTradeExecuted?: (trade: {
    pair: string
    side: 'BUY' | 'SELL'
    price: number
    amount: number
    txHash: string
  }) => void
}

export const SwapPanel: React.FC<SwapPanelProps> = ({ hasKyc, triggerKycFlow, onTradeExecuted }) => {
  const { address, isConnected } = useAccount()
  const { writeContract, data: txHash, isPending, error: writeError } = useWriteContract()
  const { isLoading: isConfirming, isSuccess } = useWaitForTransactionReceipt({ hash: txHash })

  // State
  const [tradeType, setTradeType] = useState<'BUY' | 'SELL'>('BUY') // BUY zGOLD with zKRW, or SELL zGOLD for zKRW
  const [tokenIn, setTokenIn] = useState<'zKRW' | 'zGOLD'>('zKRW')
  const [tokenOut, setTokenOut] = useState<'zKRW' | 'zGOLD'>('zGOLD')
  const [amount, setAmount] = useState<string>('')
  const [limitPrice, setLimitPrice] = useState<string>('0.0780') // zGOLD / zKRW exchange rate
  const [orderMode, setOrderMode] = useState<'LIMIT' | 'MARKET'>('LIMIT')
  const [errorMessage, setErrorMessage] = useState<string | null>(null)
  const [txSuccessMessage, setTxSuccessMessage] = useState<boolean>(false)

  // Auto-switch tokenOut when tokenIn changes
  useEffect(() => {
    if (tokenIn === 'zKRW') {
      setTokenOut('zGOLD')
      setTradeType('BUY')
    } else {
      setTokenOut('zKRW')
      setTradeType('SELL')
    }
  }, [tokenIn])

  const handleSwapTokens = () => {
    setTokenIn(tokenIn === 'zKRW' ? 'zGOLD' : 'zKRW')
  }

  const handleTradeTypeToggle = (type: 'BUY' | 'SELL') => {
    setTradeType(type)
    if (type === 'BUY') {
      setTokenIn('zKRW')
      setTokenOut('zGOLD')
    } else {
      setTokenIn('zGOLD')
      setTokenOut('zKRW')
    }
  }

  // Calculations
  const numericAmount = parseFloat(amount) || 0
  const numericPrice = parseFloat(limitPrice) || 0

  // Standard compliance fee of 15 bps (0.15%)
  const complianceFeePercent = 0.15
  const complianceFee = numericAmount * (complianceFeePercent / 100)

  // Estimated output amount
  const estimatedOutput = orderMode === 'LIMIT'
    ? (tradeType === 'BUY' ? numericAmount / numericPrice : numericAmount * numericPrice)
    : (tradeType === 'BUY' ? numericAmount / 0.0782 : numericAmount * 0.0782) // Simulated market price

  // Estimated gas
  const estimatedGas = '0.00076 ETH ($1.90)'

  // Handle submit (Swap execution)
  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    setErrorMessage(null)

    if (!isConnected) {
      setErrorMessage('Please connect your wallet first.')
      return
    }

    if (!hasKyc) {
      setErrorMessage('Bermuda KYC Passport is required for RWA transactions.')
      return
    }

    if (numericAmount <= 0) {
      setErrorMessage('Please enter a valid amount.')
      return
    }

    if (orderMode === 'LIMIT' && numericPrice <= 0) {
      setErrorMessage('Please enter a valid limit price.')
      return
    }

    try {
      // Setup contract call details
      const contractAddress = CONTRACT_ADDRESSES.baseSepolia.rwaSwap
      const tokenInAddr = tokenIn === 'zKRW' ? CONTRACT_ADDRESSES.baseSepolia.zKRW : CONTRACT_ADDRESSES.baseSepolia.zGOLD
      const tokenOutAddr = tokenOut === 'zKRW' ? CONTRACT_ADDRESSES.baseSepolia.zKRW : CONTRACT_ADDRESSES.baseSepolia.zGOLD

      // Generate a mock unique swapId
      const randomPreimage = stringToBytes(`swap-${Date.now()}-${Math.random()}`, { size: 32 })
      const swapId = keccak256(toHex(randomPreimage))

      // Convert amount to BigInt decimals (standard 18 decimals)
      const amountInDecimals = parseUnits(amount, 18)
      const minAmountOutDecimals = parseUnits(estimatedOutput.toFixed(6), 18)
      const recipient = address as `0x${string}`
      const expiration = Math.floor(Date.now() / 1000) + 3600 // 1 hour expiration

      // Execute on-chain openSwap
      writeContract({
        address: contractAddress,
        abi: RWA_SWAP_ABI,
        functionName: 'openSwap',
        args: [
          swapId,
          tokenInAddr,
          amountInDecimals,
          tokenOutAddr,
          minAmountOutDecimals,
          recipient,
          expiration,
        ],
      })
    } catch (err: any) {
      console.error(err)
      setErrorMessage(err.message || 'An error occurred setting up the transaction.')
    }
  }

  // Handle successful transaction simulation / actual execution
  useEffect(() => {
    if (isSuccess && txHash) {
      setTxSuccessMessage(true)
      setAmount('')
      if (onTradeExecuted) {
        onTradeExecuted({
          pair: 'zGOLD / zKRW',
          side: tradeType,
          price: numericPrice || 0.0782,
          amount: numericAmount,
          txHash: txHash,
        })
      }
      const timer = setTimeout(() => setTxSuccessMessage(false), 5000)
      return () => clearTimeout(timer)
    }
  }, [isSuccess, txHash])

  // Local mock trigger for testing when not connected to network with funds
  const triggerMockExecution = () => {
    if (!hasKyc) return
    if (numericAmount <= 0) return

    setTxSuccessMessage(true)
    const mockHash = '0x' + Array.from({ length: 64 }, () => Math.floor(Math.random() * 16).toString(16)).join('')
    if (onTradeExecuted) {
      onTradeExecuted({
        pair: 'zGOLD / zKRW',
        side: tradeType,
        price: numericPrice || 0.0782,
        amount: numericAmount,
        txHash: mockHash,
      })
    }
    setAmount('')
    const timer = setTimeout(() => setTxSuccessMessage(false), 5000)
    return () => clearTimeout(timer)
  }

  return (
    <div className="bg-cardBg border border-borderDark rounded-lg p-5 flex flex-col justify-between">
      <div>
        {/* Toggle Order Type */}
        <div className="flex justify-between items-center mb-5 pb-3 border-b border-borderDark">
          <div className="flex bg-darkBg rounded-md p-1">
            <button
              onClick={() => setOrderMode('LIMIT')}
              className={`px-3 py-1 text-xs font-semibold rounded transition duration-150 ${
                orderMode === 'LIMIT' ? 'bg-[#232731] text-gray-100' : 'text-gray-400 hover:text-gray-200'
              }`}
            >
              Limit Order
            </button>
            <button
              onClick={() => setOrderMode('MARKET')}
              className={`px-3 py-1 text-xs font-semibold rounded transition duration-150 ${
                orderMode === 'MARKET' ? 'bg-[#232731] text-gray-400 hover:text-gray-200' : 'text-gray-400 hover:text-gray-200'
              }`}
            >
              Market Swap
            </button>
          </div>

          <div className="flex bg-darkBg rounded-md p-1">
            <button
              onClick={() => handleTradeTypeToggle('BUY')}
              className={`px-3.5 py-1 text-xs font-semibold rounded transition duration-150 ${
                tradeType === 'BUY'
                  ? 'bg-emerald-500/20 text-emerald-400 border border-emerald-500/30'
                  : 'text-gray-400 hover:text-gray-200'
              }`}
            >
              BUY (Bid)
            </button>
            <button
              onClick={() => handleTradeTypeToggle('SELL')}
              className={`px-3.5 py-1 text-xs font-semibold rounded transition duration-150 ${
                tradeType === 'SELL'
                  ? 'bg-rose-500/20 text-rose-400 border border-rose-500/30'
                  : 'text-gray-400 hover:text-gray-200'
              }`}
            >
              SELL (Ask)
            </button>
          </div>
        </div>

        <form onSubmit={handleSubmit} className="space-y-4">
          {/* Token In Input */}
          <div className="space-y-1.5">
            <label className="text-xs font-semibold text-gray-400 flex justify-between">
              <span>Pay (Token In)</span>
              <span className="font-mono text-[10px]">Balance: 12,500.00 {tokenIn}</span>
            </label>
            <div className="flex bg-[#161920] border border-borderDark rounded-md overflow-hidden p-2 items-center focus-within:border-baseBlue">
              <input
                type="number"
                step="any"
                value={amount}
                onChange={(e) => setAmount(e.target.value)}
                placeholder="0.00"
                className="bg-transparent border-0 outline-none flex-1 text-lg font-semibold font-mono text-gray-100 placeholder-gray-600 [appearance:textfield] [&::-webkit-outer-spin-button]:appearance-none [&::-webkit-inner-spin-button]:appearance-none"
              />
              <div className="flex items-center gap-2 bg-[#232731] py-1 px-2.5 rounded text-xs font-bold text-gray-200">
                <span>{tokenIn}</span>
              </div>
            </div>
          </div>

          {/* Swap Middle Button */}
          <div className="flex justify-center -my-1">
            <button
              type="button"
              onClick={handleSwapTokens}
              className="bg-[#232731] border border-borderDark hover:border-baseBlue rounded-full p-1.5 text-gray-400 hover:text-gray-200 transition duration-150"
              title="Switch input/output tokens"
            >
              <ArrowDownUp className="w-3.5 h-3.5" />
            </button>
          </div>

          {/* Token Out Input */}
          <div className="space-y-1.5">
            <label className="text-xs font-semibold text-gray-400 flex justify-between">
              <span>Receive (Token Out)</span>
              <span className="font-mono text-[10px]">Balance: 4.80 {tokenOut}</span>
            </label>
            <div className="flex bg-[#161920] border border-borderDark rounded-md overflow-hidden p-2 items-center">
              <div className="flex-1 text-lg font-semibold font-mono text-gray-400">
                {estimatedOutput > 0 ? estimatedOutput.toLocaleString(undefined, { maximumFractionDigits: 6 }) : '0.00'}
              </div>
              <div className="flex items-center gap-2 bg-[#232731] py-1 px-2.5 rounded text-xs font-bold text-gray-200">
                <span>{tokenOut}</span>
              </div>
            </div>
          </div>

          {/* Limit Price Input */}
          {orderMode === 'LIMIT' && (
            <div className="space-y-1.5 pt-1">
              <label className="text-xs font-semibold text-gray-400 flex justify-between">
                <span>Limit Price (Rate)</span>
                <span className="text-gray-500 font-mono text-[10px]">zGOLD per zKRW</span>
              </label>
              <div className="flex bg-[#161920] border border-borderDark rounded-md overflow-hidden p-2 items-center focus-within:border-baseBlue">
                <input
                  type="number"
                  step="0.0001"
                  value={limitPrice}
                  onChange={(e) => setLimitPrice(e.target.value)}
                  placeholder="0.0000"
                  className="bg-transparent border-0 outline-none flex-1 text-base font-semibold font-mono text-gray-100 placeholder-gray-600"
                />
                <span className="text-xs text-gray-500 font-bold px-2">zKRW</span>
              </div>
            </div>
          )}

          {/* Regulatory Fee / Gas Info Panel */}
          <div className="bg-darkBg/50 rounded-md border border-borderDark p-3 space-y-2 text-xs">
            <div className="flex justify-between text-gray-400">
              <span className="flex items-center gap-1">
                Bermuda Regulatory Fee
                <span className="cursor-help" title="15 bps standard compliance levy on RWA">
                  <HelpCircle className="w-3 h-3 text-gray-500" />
                </span>
              </span>
              <span className="font-mono font-medium text-gray-200">
                {complianceFee > 0 ? `${complianceFee.toFixed(4)} ${tokenIn}` : '15 bps (0.15%)'}
              </span>
            </div>
            <div className="flex justify-between text-gray-400">
              <span className="flex items-center gap-1">Base Sepolia Gas</span>
              <span className="font-mono font-medium text-gray-200">{estimatedGas}</span>
            </div>
          </div>

          {/* Action Trigger Button */}
          {!hasKyc ? (
            <button
              type="button"
              onClick={triggerKycFlow}
              className="w-full bg-rose-500/15 border border-rose-500/30 hover:bg-rose-500/25 text-rose-300 font-bold py-3 px-4 rounded-md text-sm transition duration-150 flex items-center justify-center gap-2"
            >
              <AlertCircle className="w-4 h-4" />
              <span>Verify Bermuda RWA Passport to Unlock</span>
            </button>
          ) : (
            <div className="space-y-2">
              <button
                type="submit"
                disabled={isPending || isConfirming}
                className={`w-full font-bold py-3 px-4 rounded-md text-sm transition duration-150 flex items-center justify-center gap-2 shadow-lg ${
                  tradeType === 'BUY'
                    ? 'bg-emerald-500 hover:bg-emerald-600 text-white shadow-emerald-950/20'
                    : 'bg-rose-500 hover:bg-rose-600 text-white shadow-rose-950/20'
                }`}
              >
                {isPending || isConfirming ? (
                  <>
                    <Loader2 className="w-4 h-4 animate-spin" />
                    <span>{isPending ? 'Requesting Approval...' : 'Confirming on Base...'}</span>
                  </>
                ) : (
                  <span>
                    Place {orderMode === 'LIMIT' ? 'Limit' : 'Market'}{' '}
                    {tradeType === 'BUY' ? 'Bid' : 'Ask'}
                  </span>
                )}
              </button>

              {/* Local Mock Simulation Trigger for Offline or Sandbox environment */}
              <button
                type="button"
                onClick={triggerMockExecution}
                className="w-full text-[10px] text-gray-500 hover:text-baseBlue font-mono uppercase tracking-wider text-center pt-1 transition duration-150"
              >
                ⚡ Sandbox Instant Swap (Bypass On-chain Gas)
              </button>
            </div>
          )}
        </form>
      </div>

      {/* Messages */}
      <div className="mt-4">
        {errorMessage && (
          <div className="flex items-start gap-2 bg-rose-500/10 border border-rose-500/20 text-rose-400 p-3 rounded-md text-xs">
            <AlertCircle className="w-4 h-4 flex-shrink-0 mt-0.5" />
            <span>{errorMessage}</span>
          </div>
        )}

        {writeError && (
          <div className="flex items-start gap-2 bg-rose-500/10 border border-rose-500/20 text-rose-400 p-3 rounded-md text-xs">
            <AlertCircle className="w-4 h-4 flex-shrink-0 mt-0.5" />
            <span className="truncate">Contract Error: {writeError.message}</span>
          </div>
        )}

        {txSuccessMessage && (
          <div className="flex items-start gap-2 bg-emerald-500/10 border border-emerald-500/20 text-emerald-400 p-3 rounded-md text-xs">
            <CheckCircle className="w-4 h-4 flex-shrink-0 mt-0.5" />
            <div>
              <p className="font-semibold">Trade Order Opened</p>
              <p className="mt-0.5 text-[11px] text-emerald-400/80">
                The atomic swap request has been submitted to the Bermuda regulatory escrow on Base Sepolia.
              </p>
              {txHash && (
                <a
                  href={`https://sepolia.basescan.org/tx/${txHash}`}
                  target="_blank"
                  rel="noreferrer"
                  className="inline-block mt-2 font-mono text-[10px] text-baseBlue underline hover:text-blue-400"
                >
                  View on BaseScan ↗
                </a>
              )}
            </div>
          </div>
        )}
      </div>
    </div>
  )
}
