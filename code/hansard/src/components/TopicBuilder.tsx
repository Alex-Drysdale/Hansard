import { useState, FormEvent } from 'react'
import type { TopicGroup } from '../types'

interface TopicBuilderProps {
  onSave: (data: { name: string; keywords: string[] }) => void
  onCancel: () => void
  initialData?: TopicGroup | null
}

export default function TopicBuilder({ onSave, onCancel, initialData }: TopicBuilderProps) {
  const [name, setName] = useState(initialData?.name || '')
  const [keywordsText, setKeywordsText] = useState(
    initialData?.keywords?.join(', ') || ''
  )

  const handleSubmit = (e: FormEvent) => {
    e.preventDefault()
    const keywords = keywordsText
      .split(',')
      .map((k) => k.trim())
      .filter((k) => k)

    if (name.trim() && keywords.length > 0) {
      onSave({ name: name.trim(), keywords })
    }
  }

  return (
    <form onSubmit={handleSubmit} className="bg-white rounded-lg border border-slate-200 p-5">
      <h3 className="font-semibold text-lg text-slate-900 mb-4">
        {initialData ? 'Edit Topic Group' : 'Create Topic Group'}
      </h3>

      <div className="space-y-4">
        <div>
          <label className="block text-sm font-medium text-slate-700 mb-1">
            Topic Name
          </label>
          <input
            type="text"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="e.g., Climate & Environment"
            className="w-full px-3 py-2 border border-slate-300 rounded-lg focus:ring-2 focus:ring-slate-800 focus:border-transparent outline-none"
          />
        </div>

        <div>
          <label className="block text-sm font-medium text-slate-700 mb-1">
            Keywords (comma-separated)
          </label>
          <textarea
            value={keywordsText}
            onChange={(e) => setKeywordsText(e.target.value)}
            placeholder="climate change, net zero, carbon emissions"
            rows={3}
            className="w-full px-3 py-2 border border-slate-300 rounded-lg focus:ring-2 focus:ring-slate-800 focus:border-transparent outline-none resize-none"
          />
          <p className="text-xs text-slate-500 mt-1">
            Separate multiple keywords with commas
          </p>
        </div>
      </div>

      <div className="flex gap-3 mt-5">
        <button
          type="submit"
          className="px-4 py-2 bg-slate-800 text-white rounded-lg font-medium hover:bg-slate-700 transition-colors"
        >
          {initialData ? 'Update' : 'Create'} Topic
        </button>
        <button
          type="button"
          onClick={onCancel}
          className="px-4 py-2 text-slate-700 bg-slate-100 rounded-lg font-medium hover:bg-slate-200 transition-colors"
        >
          Cancel
        </button>
      </div>
    </form>
  )
}
