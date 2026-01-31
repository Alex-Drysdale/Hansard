// Get single member by ID
// Example: /api/member/4514 (Keir Starmer)

import { corsHeaders, handleOptions, errorResponse, jsonResponse } from '../../lib/cors.js'
import { validateMemberId, handleValidationError } from '../../lib/validation.js'

const MEMBERS_API = 'https://members-api.parliament.uk/api'

export async function onRequestGet(context) {
  try {
    // Validate member ID
    const id = validateMemberId(context.params.id)

    // Fetch member details and other useful info in parallel
    const [memberRes, contactRes, synopsisRes] = await Promise.all([
      fetch(`${MEMBERS_API}/Members/${id}`),
      fetch(`${MEMBERS_API}/Members/${id}/Contact`),
      fetch(`${MEMBERS_API}/Members/${id}/Synopsis`),
    ])

    if (!memberRes.ok) {
      return errorResponse('Member not found', 404)
    }

    const member = await memberRes.json()
    const contact = contactRes.ok ? await contactRes.json() : null
    const synopsis = synopsisRes.ok ? await synopsisRes.json() : null

    // Combine into single response
    const combined = {
      ...member,
      contact: contact?.value || null,
      synopsis: synopsis?.value || null,
    }

    return jsonResponse(combined)
  } catch (error) {
    return handleValidationError(error, corsHeaders) ||
      errorResponse(`Failed to fetch member: ${error.message}`, 500)
  }
}

export async function onRequestOptions() {
  return handleOptions()
}
