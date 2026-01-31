import { useState, useEffect, useCallback } from 'react'
import type { TopicGroup } from '../types'

const STORAGE_KEY = 'parliament-navigator-topics'

const DEFAULT_TOPICS: TopicGroup[] = [
  {
    id: '1',
    name: 'Climate & Environment',
    keywords: ['climate change', 'net zero', 'carbon emissions', 'renewable energy', 'biodiversity'],
  },
  {
    id: '2',
    name: 'Healthcare',
    keywords: ['NHS', 'healthcare', 'mental health', 'hospitals', 'doctors'],
  },
  {
    id: '3',
    name: 'Economy',
    keywords: ['inflation', 'interest rates', 'cost of living', 'GDP', 'unemployment'],
  },
]

export function useTopicGroups() {
  const [topicGroups, setTopicGroups] = useState<TopicGroup[]>(() => {
    try {
      const stored = localStorage.getItem(STORAGE_KEY)
      return stored ? JSON.parse(stored) : DEFAULT_TOPICS
    } catch {
      return DEFAULT_TOPICS
    }
  })

  useEffect(() => {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(topicGroups))
  }, [topicGroups])

  const addTopicGroup = useCallback((name: string, keywords: string[]) => {
    const newGroup: TopicGroup = {
      id: Date.now().toString(),
      name,
      keywords: keywords.filter((k) => k.trim()),
    }
    setTopicGroups((prev) => [...prev, newGroup])
    return newGroup
  }, [])

  const updateTopicGroup = useCallback((id: string, updates: Partial<TopicGroup>) => {
    setTopicGroups((prev) =>
      prev.map((group) => (group.id === id ? { ...group, ...updates } : group))
    )
  }, [])

  const deleteTopicGroup = useCallback((id: string) => {
    setTopicGroups((prev) => prev.filter((group) => group.id !== id))
  }, [])

  return {
    topicGroups,
    addTopicGroup,
    updateTopicGroup,
    deleteTopicGroup,
  }
}
