/**
 * Shared CORS helper for Cloudflare Functions
 */

/**
 * Get CORS headers for responses
 * @returns {object} Headers object with CORS headers
 */
export function corsHeaders() {
  return {
    'Access-Control-Allow-Origin': '*',
    'Content-Type': 'application/json',
  }
}

/**
 * Handle CORS preflight OPTIONS request
 * @returns {Response} Preflight response
 */
export function handleOptions() {
  return new Response(null, {
    headers: {
      'Access-Control-Allow-Origin': '*',
      'Access-Control-Allow-Methods': 'GET, OPTIONS',
      'Access-Control-Allow-Headers': 'Content-Type',
      'Access-Control-Max-Age': '86400',
    },
  })
}

/**
 * Create a JSON error response
 * @param {string} message - Error message
 * @param {number} status - HTTP status code
 * @returns {Response} Error response
 */
export function errorResponse(message, status = 500) {
  return new Response(JSON.stringify({ error: message }), {
    status,
    headers: corsHeaders(),
  })
}

/**
 * Create a JSON success response
 * @param {any} data - Response data
 * @returns {Response} Success response
 */
export function jsonResponse(data) {
  return new Response(JSON.stringify(data), {
    headers: corsHeaders(),
  })
}
