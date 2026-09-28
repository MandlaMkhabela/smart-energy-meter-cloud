# Smart Energy Meter — Cloud Backend

This moves the Flask dashboard and reading history off the local laptop and onto a cloud web service.

## Architecture

Arduino -> USB -> serial_to_mqtt.py -> Internet MQTT broker
                                      |
                                      v
                              Render cloud backend
                                      |
                                      v
                              Render PostgreSQL
                                      |
                                      v
                         Public HTTPS dashboard URL

The Arduino bridge can remain on the development laptop for now. When the Pico 2W arrives, it can publish directly to MQTT and the USB/Python bridge is removed.

## Deploy with Render Blueprint

1. Create a new GitHub repository.
2. Upload these files to the repository root:
   - app.py
   - requirements.txt
   - render.yaml
   - .gitignore
3. In Render, create a new Blueprint and select the repository.
4. Render reads `render.yaml` and creates:
   - a Python web service
   - a PostgreSQL database
5. Wait for the deployment to become Live.
6. Open the generated `https://...onrender.com` URL.
7. Keep `serial_to_mqtt.py` running on the laptop. It publishes to the same MQTT topic:
   `wits/group55/mandla-main001/data`
8. Turn off Wi-Fi on a phone and open the Render URL using mobile data. The dashboard is now genuinely remote.

## Important prototype limitations

- `broker.hivemq.com:1883` is a public, unauthenticated MQTT broker. It is suitable only for development.
- For the final secured version, move to an authenticated TLS MQTT broker, typically on port 8883.
- Render's free web service can spin down when inactive. While the dashboard is open and polling every 2 seconds, it receives regular HTTP traffic. For unattended continuous utility monitoring, use an always-on service/background worker.
- Free Render PostgreSQL is intended for development and currently expires after 30 days. Upgrade or migrate for longer-term use.

## Health check

After deployment open:

`https://YOUR-APP.onrender.com/health`

You should see JSON showing:
- `ok: true`
- `database: postgresql`
- MQTT broker and topic
