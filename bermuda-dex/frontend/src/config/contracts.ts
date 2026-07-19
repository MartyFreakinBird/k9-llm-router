export const RWA_SWAP_ABI = [
  {
    name: 'openSwap',
    type: 'function',
    stateMutability: 'nonpayable',
    inputs: [
      { name: 'swapId', type: 'bytes32' },
      { name: 'tokenIn', type: 'address' },
      { name: 'amountIn', type: 'uint256' },
      { name: 'tokenOut', type: 'address' },
      { name: 'minAmountOut', type: 'uint256' },
      { name: 'recipient', type: 'address' },
      { name: 'expiration', type: 'uint32' },
    ],
    outputs: [],
  },
  {
    name: 'claimSwap',
    type: 'function',
    stateMutability: 'nonpayable',
    inputs: [
      { name: 'swapId', type: 'bytes32' },
      { name: 'preimage', type: 'bytes32' },
    ],
    outputs: [],
  },
  {
    name: 'refund',
    type: 'function',
    stateMutability: 'nonpayable',
    inputs: [
      { name: 'swapId', type: 'bytes32' }
    ],
    outputs: [],
  },
  {
    name: 'SwapOpened',
    type: 'event',
    anonymous: false,
    inputs: [
      { name: 'swapId', type: 'bytes32', indexed: true },
      { name: 'sender', type: 'address', indexed: true },
      { name: 'tokenIn', type: 'address', indexed: false },
      { name: 'amountIn', type: 'uint256', indexed: false },
      { name: 'tokenOut', type: 'address', indexed: false },
      { name: 'minAmountOut', type: 'uint256', indexed: false },
      { name: 'recipient', type: 'address', indexed: false },
    ],
  },
] as const

export const CONTRACT_ADDRESSES = {
  baseSepolia: {
    rwaSwap: '0x32A6b4d3f1146749A322C0A74F09F0CcE65E23E0' as `0x${string}`,
    zKRW: '0x7a29D1120e2B313FBF8c1d55FFD70e309A6DF3E2' as `0x${string}`,
    zGOLD: '0x9E703a9fCcBC988D4D68a278912A3469493f0bC2' as `0x${string}`,
    kycRegistry: '0x15383FCD56bA6f6d2f3E67897dfAd70138986B67' as `0x${string}`,
  },
  base: {
    rwaSwap: '0xDEAdbeefDEADbeefDEADbeefDEADbeefDEADbeef' as `0x${string}`,
    zKRW: '0xFeedBeefFeedBeefFeedBeefFeedBeefFeedBeef' as `0x${string}`,
    zGOLD: '0xFaceBeefFaceBeefFaceBeefFaceBeefFaceBeef' as `0x${string}`,
    kycRegistry: '0xCodeBeefCodeBeefCodeBeefCodeBeefCodeBeef' as `0x${string}`,
  },
} as const
