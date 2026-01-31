import { ReactNode } from 'react'

interface EmptyStateProps {
  children: ReactNode
  className?: string
}

export default function EmptyState({ children, className = '' }: EmptyStateProps) {
  return (
    <div className={`text-center py-20 text-slate-500 ${className}`}>
      {children}
    </div>
  )
}
