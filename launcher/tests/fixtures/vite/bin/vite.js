// A test-owned process for validating identity checks without stopping real services.
const http = require('node:http')
http.createServer((_, response) => response.end('launcher test fixture')).listen(0, '127.0.0.1')
