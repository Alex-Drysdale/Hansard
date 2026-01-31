/**
 * Types for Parliament Members API
 */

export interface Party {
  id: number
  name: string
  abbreviation: string
  backgroundColour?: string
  foregroundColour?: string
}

export interface HouseMembership {
  membershipFrom: string
  membershipFromId?: number
  house: 1 | 2 // 1 = Commons, 2 = Lords
  membershipStartDate: string
  membershipEndDate?: string
  membershipEndReason?: string
  membershipEndReasonNotes?: string
  membershipStatus?: {
    statusIsActive: boolean
    statusDescription: string
    statusStartDate: string
  }
}

export interface MemberValue {
  id: number
  nameListAs: string
  nameDisplayAs: string
  nameFullTitle?: string
  nameAddressAs?: string
  latestParty?: Party
  latestHouseMembership?: HouseMembership
  gender?: string
  thumbnailUrl?: string
}

export interface MemberSearchResult {
  value: MemberValue
  links?: Array<{ rel: string; href: string }>
}

export interface MemberContact {
  type: string
  typeDescription: string
  typeId: number
  isPreferred: boolean
  isWebAddress: boolean
  notes?: string
  line1?: string
  line2?: string
  line3?: string
  line4?: string
  line5?: string
  postcode?: string
  phone?: string
  fax?: string
  email?: string
}

export interface MemberDetail extends MemberValue {
  contact?: MemberContact[] | null
  synopsis?: string | null
}

export interface WatchlistMember {
  id: number
  nameDisplayAs: string
  latestParty?: Party
  latestHouseMembership?: HouseMembership
}

export interface TopicGroup {
  id: string
  name: string
  keywords: string[]
}

export interface TopicAnalysisResult {
  id: number
  name: string
  party?: string
  constituency?: string
  count: number
}
