import { http, HttpResponse } from 'msw'

// Mock data
export const mockDebates = [
  {
    DebateSectionExtId: 'test-debate-1',
    Title: 'Test Debate',
    SittingDate: '2024-01-15T00:00:00',
    House: 'Commons',
    DebateSection: 'Main Chamber',
  },
]

export const mockMembers = {
  items: [
    {
      value: {
        id: 1234,
        nameDisplayAs: 'Test Member',
        nameListAs: 'Member, Test',
        latestParty: {
          id: 1,
          name: 'Labour',
          abbreviation: 'Lab',
        },
        latestHouseMembership: {
          membershipFrom: 'Test Constituency',
          house: 1,
          membershipStartDate: '2019-12-12',
        },
      },
    },
  ],
}

export const mockDebateDetail = {
  Overview: {
    Id: 'test-debate-1',
    Title: 'Test Debate Title',
    Date: '2024-01-15',
    House: 'Commons',
  },
  Items: [
    {
      Id: 'item-1',
      ItemType: 'Contribution',
      Value: '<p>This is a test contribution</p>',
      AttributedTo: 'Test Member',
      MemberId: 1234,
      MemberParty: 'Labour',
    },
    {
      Id: 'item-2',
      ItemType: 'Procedural',
      Value: 'Division',
    },
  ],
}

export const mockSearchResults = {
  Results: [
    {
      DebateSectionExtId: 'search-result-1',
      Title: 'Search Result Debate',
      SittingDate: '2024-01-10T00:00:00',
      House: 'Commons',
      AttributedTo: 'Another Member',
      MemberId: 5678,
      MemberParty: 'Conservative',
      TextHighlight: '<em>highlighted</em> text',
    },
  ],
  TotalResults: 1,
}

// Request handlers
export const handlers = [
  // Debates search
  http.get('/api/hansard/search/debates.json', () => {
    return HttpResponse.json({ Results: mockDebates })
  }),

  // Single debate
  http.get('/api/hansard/debates/debate/:id.json', () => {
    return HttpResponse.json(mockDebateDetail)
  }),

  // Search
  http.get('/api/hansard/search.json', () => {
    return HttpResponse.json(mockSearchResults)
  }),

  // Member contributions
  http.get('/api/hansard/search/contributions/Spoken.json', () => {
    return HttpResponse.json({ Results: [] })
  }),

  // Members search
  http.get('/api/members/Members/Search', () => {
    return HttpResponse.json(mockMembers)
  }),

  // Single member
  http.get('/api/members/Members/:id', () => {
    return HttpResponse.json({ value: mockMembers.items[0].value })
  }),
]
