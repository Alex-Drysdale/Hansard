/**
 * API response types for Parliament APIs
 */

// Generic paginated response
export interface PaginatedResponse<T> {
  items: T[]
  totalResults: number
  resultContext: string
}

// Search results wrapper
export interface SearchResponse<T> {
  Results: T[]
  TotalResults: number
  SearchTerms: string[]
}

// API error response
export interface ApiError {
  error: string
  message?: string
  status?: number
}
