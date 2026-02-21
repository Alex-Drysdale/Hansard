/**
 * Simple validation helpers for Cloudflare Functions
 * Provides input validation without external dependencies
 */

/**
 * Validate and sanitize a string parameter
 * @param {string|null} value - The value to validate
 * @param {object} options - Validation options
 * @returns {string|null} - Sanitized string or null
 */
export function validateString(value, options = {}) {
  const { maxLength = 500, required = false, pattern = null } = options

  if (value === null || value === undefined || value === '') {
    if (required) {
      throw new ValidationError(`Required parameter is missing`)
    }
    return null
  }

  if (typeof value !== 'string') {
    throw new ValidationError(`Expected string, got ${typeof value}`)
  }

  // Trim and check length
  const trimmed = value.trim()
  if (trimmed.length > maxLength) {
    throw new ValidationError(`Value exceeds maximum length of ${maxLength}`)
  }

  // Check pattern if provided
  if (pattern && !pattern.test(trimmed)) {
    throw new ValidationError(`Value does not match required pattern`)
  }

  return trimmed
}

/**
 * Validate an integer parameter
 * @param {string|number|null} value - The value to validate
 * @param {object} options - Validation options
 * @returns {number|null} - Validated number or null
 */
export function validateInt(value, options = {}) {
  const { min = 0, max = 10000, defaultValue = null, required = false } = options

  if (value === null || value === undefined || value === '') {
    if (required) {
      throw new ValidationError(`Required parameter is missing`)
    }
    return defaultValue
  }

  const num = parseInt(value, 10)
  if (isNaN(num)) {
    throw new ValidationError(`Invalid integer value`)
  }

  if (num < min || num > max) {
    throw new ValidationError(`Value must be between ${min} and ${max}`)
  }

  return num
}

/**
 * Validate a date parameter (YYYY-MM-DD format)
 * @param {string|null} value - The date string
 * @param {object} options - Validation options
 * @returns {string|null} - Validated date string or null
 */
export function validateDate(value, options = {}) {
  const { required = false } = options

  if (value === null || value === undefined || value === '') {
    if (required) {
      throw new ValidationError(`Required date parameter is missing`)
    }
    return null
  }

  // Check format YYYY-MM-DD
  const dateRegex = /^\d{4}-\d{2}-\d{2}$/
  if (!dateRegex.test(value)) {
    throw new ValidationError(`Invalid date format. Use YYYY-MM-DD`)
  }

  // Check if it's a valid date
  const date = new Date(value)
  if (isNaN(date.getTime())) {
    throw new ValidationError(`Invalid date value`)
  }

  return value
}

/**
 * Validate house parameter (Commons or Lords)
 * @param {string|null} value - The house value
 * @param {string} defaultValue - Default value if not provided
 * @returns {string} - Validated house value
 */
export function validateHouse(value, defaultValue = 'Commons') {
  if (!value) return defaultValue

  const normalized = value.charAt(0).toUpperCase() + value.slice(1).toLowerCase()
  if (normalized !== 'Commons' && normalized !== 'Lords') {
    throw new ValidationError(`House must be 'Commons' or 'Lords'`)
  }

  return normalized
}

/**
 * Validate a member ID
 * @param {string|number} value - The member ID
 * @returns {number} - Validated member ID
 */
export function validateMemberId(value) {
  const id = validateInt(value, { min: 1, max: 99999, required: true })
  return id
}

/**
 * Custom validation error class
 */
export class ValidationError extends Error {
  constructor(message) {
    super(message)
    this.name = 'ValidationError'
    this.status = 400
  }
}

/**
 * Handle validation errors and return appropriate response
 * @param {Error} error - The error to handle
 * @param {function} corsHeaders - Function to get CORS headers
 * @returns {Response} - Error response
 */
export function handleValidationError(error, corsHeaders) {
  if (error instanceof ValidationError) {
    return new Response(JSON.stringify({ error: error.message }), {
      status: 400,
      headers: corsHeaders(),
    })
  }

  // Re-throw non-validation errors
  throw error
}
