import React, { useState, useEffect } from 'react'
import { useAccount } from 'wagmi'
import { ShieldCheck, ShieldAlert, Loader2, ArrowRight } from 'lucide-react'

interface KYCStatusProps {
  kycOverride?: boolean
  onKycChange?: (status: boolean) => void
}

export const KYCStatus: React.FC<KYCStatusProps> = ({ kycOverride, onKycChange }) => {
  const { address, isConnected } = useAccount()
  const [hasValidPassport, setHasValidPassport] = useState(false)
  const [isVerifying, setIsVerifying] = useState(false)
  const [kycStep, setKycStep] = useState<number>(0) // 0: Idle, 1: Atlanta Residence, 2: Bermuda regulatory check, 3: Complete

  // Sync with override if provided
  useEffect(() => {
    if (kycOverride !== undefined) {
      setHasValidPassport(kycOverride)
    }
  }, [kycOverride])

  const triggerKycFlow = async () => {
    if (!isConnected || !address) return
    setIsVerifying(true)
    setKycStep(1)

    // Simulate Atlanta residence check
    await new Promise((resolve) => setTimeout(resolve, 1500))
    setKycStep(2)

    // Simulate Bermuda regulatory whitelist contract check
    await new Promise((resolve) => setTimeout(resolve, 1500))
    setKycStep(3)

    // Complete
    await new Promise((resolve) => setTimeout(resolve, 800))
    setHasValidPassport(true)
    setIsVerifying(false)
    setKycStep(0)
    if (onKycChange) {
      onKycChange(true)
    }
  }

  const resetKyc = () => {
    setHasValidPassport(false)
    if (onKycChange) {
      onKycChange(false)
    }
  }

  if (!isConnected || !address) {
    return (
      <div className="bg-[#1a1d24] border border-borderDark rounded-lg p-5">
        <h3 className="text-sm font-semibold uppercase tracking-wider text-gray-400 mb-3">KYC Passport Status</h3>
        <div className="flex items-center gap-3 bg-[#161920] border border-dashed border-borderDark p-4 rounded-md">
          <ShieldAlert className="w-5 h-5 text-gray-500 flex-shrink-0" />
          <div>
            <p className="text-sm font-medium text-gray-300">Wallet Disconnected</p>
            <p className="text-xs text-gray-500 mt-0.5">Connect your wallet to check Bermuda regulatory credentials.</p>
          </div>
        </div>
      </div>
    )
  }

  return (
    <div className="bg-[#1a1d24] border border-borderDark rounded-lg p-5">
      <div className="flex justify-between items-center mb-3">
        <h3 className="text-sm font-semibold uppercase tracking-wider text-gray-400">KYC Passport Status</h3>
        {hasValidPassport && (
          <button 
            onClick={resetKyc} 
            className="text-[10px] text-gray-500 hover:text-rose-400 underline transition duration-150"
          >
            Reset Passport (Testing)
          </button>
        )}
      </div>

      {hasValidPassport ? (
        <div className="bg-[#11241d] border border-emerald-900/50 p-4 rounded-md">
          <div className="flex items-start gap-3">
            <ShieldCheck className="w-5 h-5 text-emerald-400 flex-shrink-0 mt-0.5" />
            <div className="flex-1">
              <div className="flex items-center gap-2">
                <p className="text-sm font-semibold text-emerald-300">Bermuda RWA Passport Valid</p>
                <span className="bg-emerald-500/20 text-emerald-400 text-[10px] px-1.5 py-0.5 rounded font-mono font-semibold">
                  ACTIVE
                </span>
              </div>
              <p className="text-xs text-emerald-400/80 mt-1">
                Authorized for Atlanta HNWI trading. Compliance fee set to standard 15 bps (0.15%).
              </p>
              <div className="mt-2 text-[10px] text-emerald-500/60 font-mono">
                Passport ID: RWA-SBT-{address.slice(2, 10).toUpperCase()}
              </div>
            </div>
          </div>
        </div>
      ) : (
        <div className="bg-[#241215] border border-rose-950/50 p-4 rounded-md">
          <div className="flex items-start gap-3">
            <ShieldAlert className="w-5 h-5 text-rose-400 flex-shrink-0 mt-0.5" />
            <div className="flex-1">
              <div className="flex items-center gap-2">
                <p className="text-sm font-semibold text-rose-300">Regulatory Clearance Missing</p>
                <span className="bg-rose-500/20 text-rose-400 text-[10px] px-1.5 py-0.5 rounded font-mono font-semibold">
                  UNAUTHORIZED
                </span>
              </div>
              <p className="text-xs text-rose-400/80 mt-1">
                Your wallet lacks the verified Bermuda RWA passport. Atlanta HNWI accreditation required.
              </p>

              {isVerifying ? (
                <div className="mt-4 bg-[#161920] border border-borderDark p-3 rounded-md">
                  <div className="flex items-center gap-2.5 text-xs text-gray-300">
                    <Loader2 className="w-4 h-4 text-baseBlue animate-spin" />
                    <span>
                      {kycStep === 1 && "Verifying Atlanta HNWI status & residency..."}
                      {kycStep === 2 && "Checking compliance with Bermuda monetary authority..."}
                      {kycStep === 3 && "Finalizing Soulbound Token passport mint..."}
                    </span>
                  </div>
                  <div className="w-full bg-[#111] h-1 rounded overflow-hidden mt-3">
                    <div 
                      className="bg-baseBlue h-full transition-all duration-500" 
                      style={{ width: kycStep === 1 ? '33%' : kycStep === 2 ? '66%' : '100%' }}
                    />
                  </div>
                </div>
              ) : (
                <button
                  onClick={triggerKycFlow}
                  className="mt-4 flex items-center justify-center gap-1.5 w-full bg-rose-500 hover:bg-rose-600 text-white font-medium text-xs py-2 px-3 rounded transition duration-150 shadow-md shadow-rose-950/20"
                >
                  <span>Complete Bermuda KYC Passport</span>
                  <ArrowRight className="w-3.5 h-3.5" />
                </button>
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
