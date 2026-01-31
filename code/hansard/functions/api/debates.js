// Hansard Debates API
// Docs: https://hansard-api.parliament.uk/swagger/ui/index

import { corsHeaders, handleOptions, errorResponse, jsonResponse } from '../lib/cors.js'
import { validateString, validateDate, validateHouse, handleValidationError } from '../lib/validation.js'

const HANSARD_API = 'https://hansard-api.parliament.uk'

export async function onRequestGet(context) {
  const url = new URL(context.request.url)
  const params = url.searchParams

  try {
    // Validate inputs
    const date = validateDate(params.get('date'), { required: true })
    const house = validateHouse(params.get('house'), 'Commons')
    const section = validateString(params.get('section'), { maxLength: 100 })

    if (!date) {
      return errorResponse('date parameter required (YYYY-MM-DD)', 400)
    }

    // Get the daily debates overview
    const apiUrl = `${HANSARD_API}/overview/day.json?date=${date}&house=${house}`
    const response = await fetch(apiUrl)

    if (!response.ok) {
      return errorResponse('Hansard API error', response.status)
    }

    const data = await response.json()

    // Filter by section if provided
    if (section && data.Sections) {
      data.Sections = data.Sections.filter(s =>
        s.Type?.toLowerCase().includes(section.toLowerCase())
      )
    }

    return jsonResponse(data)
  } catch (error) {
    return handleValidationError(error, corsHeaders) ||
      errorResponse(`Failed to fetch debates: ${error.message}`, 500)
  }
}

export async function onRequestOptions() {
  return handleOptions()
}
