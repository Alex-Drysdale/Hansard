import type { MemberSearchResult, MemberValue } from '../types'

const BASE_URL = '/api/members'

export async function searchMembers(
  name: string,
  take: number = 20
): Promise<MemberSearchResult[]> {
  const params = new URLSearchParams({
    Name: name,
    take: take.toString(),
    IsCurrentMember: 'true',
  })

  const res = await fetch(`${BASE_URL}/Members/Search?${params}`)
  if (!res.ok) throw new Error('Failed to search members')
  const data = await res.json()
  return data.items || []
}

export async function fetchMember(id: number | string): Promise<MemberValue> {
  const res = await fetch(`${BASE_URL}/Members/${id}`)
  if (!res.ok) throw new Error('Member not found')
  const data = await res.json()
  return data.value
}

export async function fetchMemberSynopsis(id: number | string): Promise<string | null> {
  const res = await fetch(`${BASE_URL}/Members/${id}/Synopsis`)
  if (!res.ok) return null
  const data = await res.json()
  return data.value
}

export function getMemberPhotoUrl(memberId: number | string): string {
  return `https://members-api.parliament.uk/api/Members/${memberId}/Thumbnail`
}

// Party colors based on UK political party branding
const PARTY_COLORS: Record<string, string> = {
  labour: '#E4003B',
  conservative: '#0087DC',
  'liberal democrat': '#FAA61A',
  green: '#6AB023',
  'scottish national': '#FDF38E',
  snp: '#FDF38E',
  plaid: '#005B54',
  dup: '#D46A4C',
  sinn: '#326760',
}

export function getPartyColor(partyName: string | undefined): string {
  if (!partyName) return '#808080'

  const name = partyName.toLowerCase()
  for (const [key, color] of Object.entries(PARTY_COLORS)) {
    if (name.includes(key)) return color
  }
  return '#808080'
}
