export interface NowPlaying {
  isPlaying: boolean;
  track?: string;
  artist?: string;
  album?: string;
  albumArt?: string;
  url?: string;
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
  '@attr'?: { nowplaying: string };
}

interface LastFmRecentTracksResponse {
  recenttracks: {
    track: LastFmTrack[];
  };
}

const CACHE_TTL_MS = 20_000;
let cache: { data: NowPlaying; expiresAt: number } | null = null;

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
  url.searchParams.set('limit', '1');

  const response = await fetch(url);
  if (!response.ok) {
    throw new Error(`last.fm API returned ${response.status}`);
  }

  const body = (await response.json()) as LastFmRecentTracksResponse;
  const track = body.recenttracks?.track?.[0];

  const data: NowPlaying = track
    ? {
        isPlaying: track['@attr']?.nowplaying === 'true',
        track: track.name,
        artist: track.artist['#text'],
        album: track.album['#text'] || undefined,
        albumArt: track.image?.find((image) => image.size === 'large')?.['#text'] || undefined,
        url: track.url,
      }
    : { isPlaying: false };

  cache = { data, expiresAt: Date.now() + CACHE_TTL_MS };
  return data;
}
