import { ReactNode } from 'react'

interface ErrorMessageProps {
  children: ReactNode
  className?: string
}

export default function ErrorMessage({ children, className = '' }: ErrorMessageProps) {
  return (
    <div
      className={`bg-red-50 border border-red-200 text-red-700 px-4 py-3 rounded-lg ${className}`}
      role="alert"
    >
      {children}
    </div>
  )
}
