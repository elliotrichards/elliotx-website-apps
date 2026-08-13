import { createServer } from 'node:http';
import { getNowPlaying } from './lastfm';

const port = Number(process.env.PORT) || 8080;

const server = createServer(async (req, res) => {
  // Read-only public data, no cookies/auth involved — safe to allow any origin.
  res.setHeader('Access-Control-Allow-Origin', '*');

  if (req.method === 'OPTIONS') {
    res.writeHead(204);
    res.end();
    return;
  }

  if (req.method !== 'GET') {
    res.writeHead(405, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ error: 'method not allowed' }));
    return;
  }

  try {
    const data = await getNowPlaying();
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify(data));
  } catch (err) {
    console.error(err);
    res.writeHead(502, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ error: 'failed to fetch now-playing data' }));
  }
});

server.listen(port, () => {
  console.log(`listening on ${port}`);
});
