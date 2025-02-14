

const http = require('http');

// Create the server
const server = http.createServer((req, res) => {
    let body = '';

    // Collect the incoming data
    req.on('data', chunk => {
        body += chunk;
        console.log(body)
        console.log("Received, but not finished")
    });

    // When the data is fully received
    req.on('end', () => {
        console.log(body)
        res.writeHead(200, { 'Content-Type': 'text/plain' });
        res.end(body.trim());
    });
});

// Listen on port 8080
server.listen(8080, () => {
    console.log('Server is running on http://localhost:8080');
});
