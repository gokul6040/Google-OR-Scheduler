// Only fictional inputs and simulation-only credentials belong in this mock.
// Responses model Meta's messages/error envelope; this is a local fault fixture,
// not an owner-published mock or an exhaustive provider contract.
const http = require('http');
http.createServer((req, res) => {
  let raw = '';
  req.on('data', chunk => raw += chunk);
  req.on('end', () => {
    res.setHeader('Content-Type', 'application/json');
    if (req.method !== 'POST' || !/^\/v26\.0\/123456789\/messages$/.test(req.url)) {
      res.writeHead(501); res.end(JSON.stringify({error:{message:'Not modeled'}})); return;
    }
    let body;
    try { body = typeof req.body === 'object' ? req.body : JSON.parse(raw); }
    catch (_) { res.writeHead(400); res.end('{}'); return; }
    const mode = body.text?.body;
    const fail = (code, status=400) => {
      res.writeHead(status);
      res.end(JSON.stringify({error:{code, message:'Fictional failure; no patient information'}}));
    };
    if (mode === 'temporary') { res.setHeader('Retry-After', '45'); fail(130429); }
    else if (mode === 'expired') fail(190, 401);
    else if (mode === 'invalid-template') fail(132001);
    else if (mode === 'permanent') fail(131026);
    else if (mode === 'gateway') { res.writeHead(503); res.end('<html>Unavailable</html>'); }
    else if (mode === 'timeout') { setTimeout(() => res.end(JSON.stringify({messages:[{id:'wamid.late'}]})), 1500); }
    else res.end(JSON.stringify({messaging_product:'whatsapp', messages:[{id:'wamid.simulated'}]}));
  });
}).listen(process.env.PORT);
