import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import SearchHighlight from '../SearchHighlight'

describe('SearchHighlight', () => {
  it('renders plain text without highlighting when no search term', () => {
    render(<SearchHighlight text="Hello world" />)
    expect(screen.getByText('Hello world')).toBeInTheDocument()
  })

  it('renders null for empty text', () => {
    const { container } = render(<SearchHighlight text="" />)
    expect(container.firstChild).toBeNull()
  })

  it('highlights matching text case-insensitively', () => {
    render(<SearchHighlight text="Hello World" searchTerm="world" />)
    const mark = screen.getByText('World')
    expect(mark.tagName).toBe('MARK')
  })

  it('sanitizes dangerous HTML content', () => {
    const dangerousHtml = '<script>alert("xss")</script><p>Safe content</p>'
    render(<SearchHighlight text={dangerousHtml} />)

    // Script should be removed
    expect(screen.queryByText('alert("xss")')).not.toBeInTheDocument()
    // Safe content should remain
    expect(screen.getByText('Safe content')).toBeInTheDocument()
  })

  it('removes dangerous attributes from HTML', () => {
    const htmlWithHandler = '<p onclick="alert(1)">Click me</p>'
    const { container } = render(<SearchHighlight text={htmlWithHandler} />)

    const p = container.querySelector('p')
    expect(p).not.toHaveAttribute('onclick')
    expect(screen.getByText('Click me')).toBeInTheDocument()
  })

  it('allows safe HTML tags', () => {
    const safeHtml = '<strong>Bold</strong> and <em>italic</em>'
    render(<SearchHighlight text={safeHtml} />)

    expect(screen.getByText('Bold').tagName).toBe('STRONG')
    expect(screen.getByText('italic').tagName).toBe('EM')
  })

  it('handles highlighting in HTML content', () => {
    const html = '<p>This contains test text</p>'
    const { container } = render(<SearchHighlight text={html} searchTerm="test" />)

    const mark = container.querySelector('mark')
    expect(mark).toBeInTheDocument()
    expect(mark?.textContent).toBe('test')
  })

  it('escapes regex special characters in search term', () => {
    const text = 'Price: $100 (special)'
    // This should not crash with regex special chars
    render(<SearchHighlight text={text} searchTerm="$100" />)
    const mark = screen.getByText('$100')
    expect(mark.tagName).toBe('MARK')
  })

  it('handles multiple matches in text', () => {
    const text = 'The cat sat on the mat with another cat'
    const { container } = render(<SearchHighlight text={text} searchTerm="cat" />)

    const marks = container.querySelectorAll('mark')
    expect(marks).toHaveLength(2)
  })
})
