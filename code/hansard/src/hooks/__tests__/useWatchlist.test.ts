import { describe, it, expect, beforeEach, vi } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { useWatchlist } from '../useWatchlist'

// Mock localStorage
const localStorageMock = {
  store: {} as Record<string, string>,
  getItem: vi.fn((key: string) => localStorageMock.store[key] || null),
  setItem: vi.fn((key: string, value: string) => {
    localStorageMock.store[key] = value
  }),
  removeItem: vi.fn((key: string) => {
    delete localStorageMock.store[key]
  }),
  clear: vi.fn(() => {
    localStorageMock.store = {}
  }),
}

Object.defineProperty(window, 'localStorage', { value: localStorageMock })

describe('useWatchlist', () => {
  const mockMember = {
    id: 1234,
    nameDisplayAs: 'Test Member',
    latestParty: { id: 1, name: 'Labour', abbreviation: 'Lab' },
    latestHouseMembership: {
      membershipFrom: 'Test Constituency',
      house: 1 as const,
      membershipStartDate: '2019-12-12',
    },
  }

  beforeEach(() => {
    localStorageMock.clear()
    localStorageMock.getItem.mockClear()
    localStorageMock.setItem.mockClear()
  })

  it('initializes with empty watchlist', () => {
    const { result } = renderHook(() => useWatchlist())

    expect(result.current.watchlist).toEqual([])
  })

  it('loads existing watchlist from localStorage', () => {
    localStorageMock.store['parliament-navigator-watchlist'] = JSON.stringify([mockMember])

    const { result } = renderHook(() => useWatchlist())

    expect(result.current.watchlist).toHaveLength(1)
    expect(result.current.watchlist[0].nameDisplayAs).toBe('Test Member')
  })

  it('adds member to watchlist', () => {
    const { result } = renderHook(() => useWatchlist())

    act(() => {
      result.current.addToWatchlist(mockMember)
    })

    expect(result.current.watchlist).toHaveLength(1)
    expect(result.current.watchlist[0].id).toBe(1234)
  })

  it('prevents duplicate members', () => {
    const { result } = renderHook(() => useWatchlist())

    act(() => {
      result.current.addToWatchlist(mockMember)
      result.current.addToWatchlist(mockMember)
    })

    expect(result.current.watchlist).toHaveLength(1)
  })

  it('removes member from watchlist', () => {
    const { result } = renderHook(() => useWatchlist())

    act(() => {
      result.current.addToWatchlist(mockMember)
    })

    expect(result.current.watchlist).toHaveLength(1)

    act(() => {
      result.current.removeFromWatchlist(1234)
    })

    expect(result.current.watchlist).toHaveLength(0)
  })

  it('correctly checks if member is in watchlist', () => {
    const { result } = renderHook(() => useWatchlist())

    expect(result.current.isInWatchlist(1234)).toBe(false)

    act(() => {
      result.current.addToWatchlist(mockMember)
    })

    expect(result.current.isInWatchlist(1234)).toBe(true)
    expect(result.current.isInWatchlist(9999)).toBe(false)
  })

  it('persists changes to localStorage', () => {
    const { result } = renderHook(() => useWatchlist())

    act(() => {
      result.current.addToWatchlist(mockMember)
    })

    expect(localStorageMock.setItem).toHaveBeenCalledWith(
      'parliament-navigator-watchlist',
      expect.stringContaining('"id":1234')
    )
  })

  it('handles corrupted localStorage gracefully', () => {
    localStorageMock.store['parliament-navigator-watchlist'] = 'invalid json'

    const { result } = renderHook(() => useWatchlist())

    expect(result.current.watchlist).toEqual([])
  })
})
