"""Tools MCP expuestas por mcp-server.

Para iteración 1: solo `gmail_read` mockeada con respuesta fija.
En iteración 4 sumamos gmail_send, calendar_list, calendar_create.

Si GOOGLE_REAL=1 en env, se usa Google API real con creds en
GOOGLE_CREDS_PATH. Si no, MockGmailTool devuelve datos fijos.
"""
