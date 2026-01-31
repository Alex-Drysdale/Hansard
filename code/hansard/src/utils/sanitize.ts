import DOMPurify from 'dompurify'

/**
 * Sanitize HTML content to prevent XSS attacks.
 * Allows safe HTML tags while stripping dangerous content.
 */
export function sanitizeHtml(html: string): string {
  if (!html) return ''

  return DOMPurify.sanitize(html, {
    ALLOWED_TAGS: [
      'b', 'i', 'em', 'strong', 'a', 'p', 'br', 'ul', 'ol', 'li',
      'span', 'div', 'mark', 'sub', 'sup', 'blockquote'
    ],
    ALLOWED_ATTR: ['href', 'class', 'target', 'rel'],
    ALLOW_DATA_ATTR: false,
  })
}

/**
 * Sanitize text and apply search highlighting.
 * First sanitizes the HTML, then safely applies the highlight.
 */
export function sanitizeAndHighlight(html: string, searchTerm: string): string {
  // First sanitize the HTML
  const clean = sanitizeHtml(html)

  if (!searchTerm) return clean

  // Escape regex special characters in search term
  const escapedTerm = searchTerm.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')

  // Apply highlighting to the sanitized content
  const regex = new RegExp(`(${escapedTerm})`, 'gi')
  return clean.replace(regex, '<mark class="highlight-match">$1</mark>')
}
