import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'

// We need to import the module dynamically to mock fetch before it loads
describe('hansardApi', () => {
  let mockFetch: ReturnType<typeof vi.fn>
  let originalFetch: typeof globalThis.fetch
  let hansardApi: typeof import('../hansardApi')

  beforeEach(async () => {
    // Save original fetch
    originalFetch = globalThis.fetch

    // Create mock
    mockFetch = vi.fn()
    // @ts-expect-error - mocking fetch for tests
    globalThis.fetch = mockFetch

    // Re-import module to get fresh instance
    vi.resetModules()
    hansardApi = await import('../hansardApi')
  })

  afterEach(() => {
    // Restore original fetch
    globalThis.fetch = originalFetch
    vi.resetAllMocks()
  })

  describe('fetchRecentDebates', () => {
    it('fetches recent debates with default parameters', async () => {
      mockFetch.mockResolvedValueOnce({
        ok: true,
        json: async () => ({ Results: [{ Title: 'Test Debate' }] }),
      })

      const result = await hansardApi.fetchRecentDebates()

      expect(mockFetch).toHaveBeenCalledOnce()
      expect(mockFetch.mock.calls[0][0]).toContain('/api/hansard/search/debates.json')
      expect(result).toEqual([{ Title: 'Test Debate' }])
    })

    it('includes house filter when provided', async () => {
      mockFetch.mockResolvedValueOnce({
        ok: true,
        json: async () => ({ Results: [] }),
      })

      await hansardApi.fetchRecentDebates('Commons')

      expect(mockFetch).toHaveBeenCalledOnce()
      expect(mockFetch.mock.calls[0][0]).toContain('house=Commons')
    })

    it('throws error on failed request', async () => {
      mockFetch.mockResolvedValueOnce({ ok: false })

      await expect(hansardApi.fetchRecentDebates()).rejects.toThrow('Failed to fetch debates')
    })
  })

  describe('fetchDebate', () => {
    it('fetches a single debate by ID', async () => {
      const mockDebate = { Overview: { Title: 'Test' }, Items: [] }
      mockFetch.mockResolvedValueOnce({
        ok: true,
        json: async () => mockDebate,
      })

      const result = await hansardApi.fetchDebate('test-id')

      expect(mockFetch).toHaveBeenCalledWith('/api/hansard/debates/debate/test-id.json')
      expect(result).toEqual(mockDebate)
    })

    it('throws error when debate not found', async () => {
      mockFetch.mockResolvedValueOnce({ ok: false })

      await expect(hansardApi.fetchDebate('invalid-id')).rejects.toThrow('Failed to fetch debate')
    })
  })

  describe('searchHansard', () => {
    it('searches with default options', async () => {
      mockFetch.mockResolvedValueOnce({
        ok: true,
        json: async () => ({ Results: [{ Title: 'Result' }] }),
      })

      const result = await hansardApi.searchHansard('climate')

      expect(mockFetch).toHaveBeenCalledOnce()
      expect(mockFetch.mock.calls[0][0]).toContain('searchTerm=climate')
      expect(result).toEqual([{ Title: 'Result' }])
    })

    it('includes all search options', async () => {
      mockFetch.mockResolvedValueOnce({
        ok: true,
        json: async () => ({ Results: [] }),
      })

      await hansardApi.searchHansard('test', {
        take: 50,
        memberId: 1234,
        startDate: '2024-01-01',
        endDate: '2024-01-31',
      })

      const url = mockFetch.mock.calls[0][0]
      expect(url).toContain('take=50')
      expect(url).toContain('memberId=1234')
      expect(url).toContain('startDate=2024-01-01')
      expect(url).toContain('endDate=2024-01-31')
    })
  })

  describe('fetchMemberContributions', () => {
    it('fetches contributions for a member', async () => {
      mockFetch.mockResolvedValueOnce({
        ok: true,
        json: async () => ({ Results: [{ DebateSection: 'Test' }] }),
      })

      const result = await hansardApi.fetchMemberContributions(1234)

      expect(mockFetch).toHaveBeenCalledOnce()
      expect(mockFetch.mock.calls[0][0]).toContain('memberId=1234')
      expect(result).toEqual([{ DebateSection: 'Test' }])
    })
  })
})
