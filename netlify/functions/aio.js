export default async (req, context) => {
  const AIO_USER = process.env.AIO_USER;
  const AIO_KEY  = process.env.AIO_KEY;

  if (!AIO_USER || !AIO_KEY) {
    return new Response(JSON.stringify({ error: "Missing AIO_USER/AIO_KEY env vars" }), {
      status: 500,
      headers: { "Content-Type": "application/json" }
    });
  }

  const url = new URL(req.url);
  // client calls: /.netlify/functions/aio?path=/feeds/xyz/data/last
  const path = url.searchParams.get("path");
  if (!path || !path.startsWith("/")) {
    return new Response(JSON.stringify({ error: "Missing/invalid path" }), {
      status: 400,
      headers: { "Content-Type": "application/json" }
    });
  }

  const upstream = `https://io.adafruit.com/api/v2/${encodeURIComponent(AIO_USER)}${path}`;

  const r = await fetch(upstream, {
    headers: { "X-AIO-Key": AIO_KEY }
  });

  const body = await r.text();
  return new Response(body, {
    status: r.status,
    headers: {
      "Content-Type": r.headers.get("content-type") || "application/json",
      // allow your site to call this function
      "Access-Control-Allow-Origin": "*"
    }
  });
};
