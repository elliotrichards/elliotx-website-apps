export interface RecentTrack {
  isPlaying: boolean;
  track: string;
  artist: string;
  album?: string;
  albumArt?: string;
  url: string;
  // Unix seconds the track was scrobbled. Absent for the currently-playing
  // track — last.fm doesn't timestamp it until it's scrobbled.
  date?: number;
}

export interface NowPlaying {
  isPlaying: boolean;
  track?: string;
  artist?: string;
  album?: string;
  albumArt?: string;
  url?: string;
  // Up to RECENT_TRACKS_LIMIT most recent tracks, newest first (mirrors
  // last.fm's own ordering). Powers the homepage's "last played" list.
  recentTracks: RecentTrack[];
}

interface LastFmImage {
  '#text': string;
  size: string;
}

interface LastFmTrack {
  name: string;
  url: string;
  artist: { '#text': string };
  album: { '#text': string };
  image: LastFmImage[];
  date?: { uts: string };
  '@attr'?: { nowplaying: string };
}

interface LastFmRecentTracksResponse {
  recenttracks: {
    track: LastFmTrack[];
  };
}

const CACHE_TTL_MS = 20_000;
const RECENT_TRACKS_LIMIT = 10;
let cache: { data: NowPlaying; expiresAt: number } | null = null;

function toRecentTrack(track: LastFmTrack): RecentTrack {
  return {
    isPlaying: track['@attr']?.nowplaying === 'true',
    track: track.name,
    artist: track.artist['#text'],
    album: track.album['#text'] || undefined,
    albumArt: track.image?.find((image) => image.size === 'large')?.['#text'] || undefined,
    url: track.url,
    date: track.date ? Number(track.date.uts) : undefined,
  };
}

// last.fm's own uptime/rate limits are out of our hands, and this endpoint
// is public and unauthenticated — a short cache absorbs both concerns
// without the widget ever seeing stale-for-more-than-20s data.
export async function getNowPlaying(): Promise<NowPlaying> {
  if (cache && cache.expiresAt > Date.now()) {
    return cache.data;
  }

  const apiKey = process.env.LASTFM_API_KEY;
  const username = process.env.LASTFM_USERNAME;
  if (!apiKey || !username) {
    throw new Error('LASTFM_API_KEY and LASTFM_USERNAME must be set');
  }

  const url = new URL('https://ws.audioscrobbler.com/2.0/');
  url.searchParams.set('method', 'user.getrecenttracks');
  url.searchParams.set('user', username);
  url.searchParams.set('api_key', apiKey);
  url.searchParams.set('format', 'json');
  url.searchParams.set('limit', String(RECENT_TRACKS_LIMIT));

  const response = await fetch(url);
  if (!response.ok) {
    throw new Error(`last.fm API returned ${response.status}`);
  }

  const body = (await response.json()) as LastFmRecentTracksResponse;
  const tracks = body.recenttracks?.track ?? [];
  const recentTracks = tracks.map(toRecentTrack);
  const first = recentTracks[0];

  const data: NowPlaying = first
    ? {
        isPlaying: first.isPlaying,
        track: first.track,
        artist: first.artist,
        album: first.album,
        albumArt: first.albumArt,
        url: first.url,
        recentTracks,
      }
    : { isPlaying: false, recentTracks: [] };

  cache = { data, expiresAt: Date.now() + CACHE_TTL_MS };
  return data;
}
