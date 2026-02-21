interface LoadingSpinnerProps {
  size?: 'sm' | 'md' | 'lg'
  className?: string
}

const sizeClasses = {
  sm: 'h-6 w-6 border-2',
  md: 'h-8 w-8 border-4',
  lg: 'h-10 w-10 border-4',
}

export default function LoadingSpinner({ size = 'md', className = '' }: LoadingSpinnerProps) {
  return (
    <div className={`flex items-center justify-center ${className}`}>
      <div
        className={`animate-spin rounded-full border-slate-200 border-t-slate-800 ${sizeClasses[size]}`}
      />
    </div>
  )
}
