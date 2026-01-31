// Parliament Bills API
// Docs: https://bills-api.parliament.uk/index.html

import { corsHeaders, handleOptions, errorResponse, jsonResponse } from '../lib/cors.js'
import { validateString, validateInt, handleValidationError } from '../lib/validation.js'

const BILLS_API = 'https://bills-api.parliament.uk/api/v1'

// Valid sort orders for the Bills API
const VALID_SORT_ORDERS = [
  'DateUpdatedDescending',
  'DateUpdatedAscending',
  'TitleAscending',
  'TitleDescending',
]

export async function onRequestGet(context) {
  const url = new URL(context.request.url)
  const params = url.searchParams

  try {
    // Validate inputs
    const searchTerm = validateString(params.get('search'), { maxLength: 200 }) || ''
    const skip = validateInt(params.get('skip'), { min: 0, max: 10000, defaultValue: 0 })
    const take = validateInt(params.get('take'), { min: 1, max: 100, defaultValue: 20 })
    const currentHouse = validateString(params.get('house'), { maxLength: 20 }) || ''

    // Validate sort order
    let sortOrder = validateString(params.get('sort'), { maxLength: 50 }) || 'DateUpdatedDescending'
    if (!VALID_SORT_ORDERS.includes(sortOrder)) {
      sortOrder = 'DateUpdatedDescending'
    }

    const apiUrl = new URL(`${BILLS_API}/Bills`)
    if (searchTerm) apiUrl.searchParams.set('SearchTerm', searchTerm)
    apiUrl.searchParams.set('Skip', skip.toString())
    apiUrl.searchParams.set('Take', take.toString())
    if (currentHouse) apiUrl.searchParams.set('CurrentHouse', currentHouse)
    apiUrl.searchParams.set('SortOrder', sortOrder)

    const response = await fetch(apiUrl.toString())

    if (!response.ok) {
      return errorResponse('Bills API error', response.status)
    }

    const data = await response.json()
    return jsonResponse(data)
  } catch (error) {
    return handleValidationError(error, corsHeaders) ||
      errorResponse(`Failed to fetch bills: ${error.message}`, 500)
  }
}

export async function onRequestOptions() {
  return handleOptions()
}
