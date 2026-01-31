// Hansard Search API - search debate transcripts
// Docs: https://hansard-api.parliament.uk/swagger/ui/index

import { corsHeaders, handleOptions, errorResponse, jsonResponse } from '../lib/cors.js'
import { validateString, validateInt, validateDate, validateHouse, handleValidationError } from '../lib/validation.js'

const HANSARD_API = 'https://hansard-api.parliament.uk'

export async function onRequestGet(context) {
  const url = new URL(context.request.url)
  const params = url.searchParams

  try {
    // Validate inputs
    const query = validateString(params.get('q') || params.get('query'), {
      required: true,
      maxLength: 200,
    })
    const house = validateHouse(params.get('house'), 'Commons')
    const startDate = validateDate(params.get('startDate'))
    const endDate = validateDate(params.get('endDate'))
    const member = validateInt(params.get('member'), { min: 1, max: 99999 })
    const skip = validateInt(params.get('skip'), { min: 0, max: 10000, defaultValue: 0 })
    const take = validateInt(params.get('take'), { min: 1, max: 100, defaultValue: 20 })

    if (!query) {
      return errorResponse('q or query parameter required', 400)
    }

    const apiUrl = new URL(`${HANSARD_API}/search.json`)
    apiUrl.searchParams.set('q', query)
    apiUrl.searchParams.set('house', house)
    apiUrl.searchParams.set('skip', skip.toString())
    apiUrl.searchParams.set('take', take.toString())

    if (startDate) apiUrl.searchParams.set('startDate', startDate)
    if (endDate) apiUrl.searchParams.set('endDate', endDate)
    if (member) apiUrl.searchParams.set('member', member.toString())

    const response = await fetch(apiUrl.toString())

    if (!response.ok) {
      return errorResponse('Hansard search error', response.status)
    }

    const data = await response.json()
    return jsonResponse(data)
  } catch (error) {
    return handleValidationError(error, corsHeaders) ||
      errorResponse(`Failed to search Hansard: ${error.message}`, 500)
  }
}

export async function onRequestOptions() {
  return handleOptions()
}
