// Parliament Members API
// Docs: https://members-api.parliament.uk/index.html

import { corsHeaders, handleOptions, errorResponse, jsonResponse } from '../lib/cors.js'
import { validateString, validateInt, handleValidationError } from '../lib/validation.js'

const MEMBERS_API = 'https://members-api.parliament.uk/api'

export async function onRequestGet(context) {
  const url = new URL(context.request.url)
  const params = url.searchParams

  try {
    // Validate inputs
    const name = validateString(params.get('name'), { maxLength: 100 }) || ''
    const skip = validateInt(params.get('skip'), { min: 0, max: 10000, defaultValue: 0 })
    const take = validateInt(params.get('take'), { min: 1, max: 100, defaultValue: 20 })
    const house = validateString(params.get('house'), { maxLength: 20 }) || ''
    const isCurrentMember = params.get('current') !== 'false' ? 'true' : 'false'

    // Build Parliament API URL
    const apiUrl = new URL(`${MEMBERS_API}/Members/Search`)
    if (name) apiUrl.searchParams.set('Name', name)
    apiUrl.searchParams.set('skip', skip.toString())
    apiUrl.searchParams.set('take', take.toString())
    if (house) apiUrl.searchParams.set('House', house)
    apiUrl.searchParams.set('IsCurrentMember', isCurrentMember)

    const response = await fetch(apiUrl.toString())

    if (!response.ok) {
      return errorResponse('Parliament API error', response.status)
    }

    const data = await response.json()
    return jsonResponse(data)
  } catch (error) {
    return handleValidationError(error, corsHeaders) ||
      errorResponse(`Failed to fetch members: ${error.message}`, 500)
  }
}

export async function onRequestOptions() {
  return handleOptions()
}
