 # Step-by-Step (if you want to see what's happening):

 # 1. Pull latest code:
ssh teslaproxy
cd /opt/solar-charging-backend
git pull
docker compose down
docker compose build --no-cache
docker compose up -d

# 2. Verify it's running:
 docker compose ps
 docker compose logs -f

# 3. Test the new endpoint:
 curl http://localhost:8088/

