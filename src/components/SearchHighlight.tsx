import { sanitizeHtml, sanitizeAndHighlight } from '../utils/sanitize'

interface SearchHighlightProps {
  text: string
  searchTerm?: string
}

export default function SearchHighlight({ text, searchTerm }: SearchHighlightProps) {
  if (!text) {
    return null
  }

  // If no search term, just sanitize and render
  if (!searchTerm) {
    // Check if it's HTML content
    if (text.includes('<')) {
      return <span dangerouslySetInnerHTML={{ __html: sanitizeHtml(text) }} />
    }
    // Plain text - no dangerouslySetInnerHTML needed
    return <span>{text}</span>
  }

  // Check if it's HTML content
  if (text.includes('<')) {
    // Sanitize and highlight
    const highlighted = sanitizeAndHighlight(text, searchTerm)
    return <span dangerouslySetInnerHTML={{ __html: highlighted }} />
  }

  // For plain text, split and highlight without using dangerouslySetInnerHTML
  const escapedTerm = searchTerm.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
  const parts = text.split(new RegExp(`(${escapedTerm})`, 'gi'))

  return (
    <span>
      {parts.map((part, i) =>
        part.toLowerCase() === searchTerm.toLowerCase() ? (
          <mark key={i} className="highlight-match">
            {part}
          </mark>
        ) : (
          <span key={i}>{part}</span>
        )
      )}
    </span>
  )
}
