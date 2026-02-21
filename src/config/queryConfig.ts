/**
 * Standardized React Query configuration
 * Consistent caching settings across the application
 */

export const QUERY_CONFIG = {
  // Debates and transcripts - moderate freshness needed
  debates: {
    staleTime: 5 * 60 * 1000, // 5 minutes
    gcTime: 30 * 60 * 1000, // 30 minutes
  },

  // Member information - can be cached longer
  members: {
    staleTime: 15 * 60 * 1000, // 15 minutes
    gcTime: 60 * 60 * 1000, // 60 minutes
  },

  // Search results - shorter cache for dynamic content
  search: {
    staleTime: 2 * 60 * 1000, // 2 minutes
    gcTime: 10 * 60 * 1000, // 10 minutes
  },

  // Topic analysis - combines multiple searches
  topics: {
    staleTime: 5 * 60 * 1000, // 5 minutes
    gcTime: 30 * 60 * 1000, // 30 minutes
  },
} as const

/**
 * Default query client options with exponential backoff
 */
export const DEFAULT_QUERY_OPTIONS = {
  queries: {
    refetchOnWindowFocus: false,
    retry: 3,
    retryDelay: (attemptIndex: number) => Math.min(1000 * 2 ** attemptIndex, 30000),
  },
} as const
