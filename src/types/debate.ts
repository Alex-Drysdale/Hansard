/**
 * Types for Hansard Debates API
 */

export interface DebateOverview {
  Id: string
  Title: string
  Date: string
  House: 'Commons' | 'Lords'
  Location?: string
  SittingDate?: string
}

export interface DebateItem {
  Id: string
  ItemType: 'Contribution' | 'Procedural' | 'Division' | 'Time'
  Value?: string
  AttributedTo?: string
  MemberId?: number
  MemberParty?: string
  MemberConstituency?: string
  Timecode?: string
  OrderIndex?: number
}

export interface Debate {
  Overview: DebateOverview
  Items: DebateItem[]
}

export interface DebateSearchResult {
  DebateSectionExtId: string
  Title: string
  SittingDate: string
  House: 'Commons' | 'Lords'
  DebateSection?: string
  AttributedTo?: string
  MemberId?: number
  MemberParty?: string
  MemberConstituency?: string
  TextHighlight?: string
}

export interface Speaker {
  id?: number
  name: string
  party?: string
  constituency?: string
  count: number
}

export interface ContributionResult {
  DebateSectionExtId: string
  DebateSection: string
  SittingDate: string
  House?: string
  TextHighlight?: string
  AttributedTo?: string
}
