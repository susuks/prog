module.exports = {
  apps: [
    {
      name: "autocred-site-api",
      script: "site_main.py",
      interpreter: "./venv/bin/python",
      instances: 1,
      autorestart: true,
      watch: false,
      max_memory_restart: "600M",
      env: {
        PYTHONUNBUFFERED: "1",
      },
    },
    {
      name: "autocred-cda-worker",
      script: "api_cda_main.py",
      interpreter: "./venv/bin/python",
      instances: 1,
      autorestart: true,
      watch: false,
      max_memory_restart: "600M",
      env: {
        PYTHONUNBUFFERED: "1",
      },
    },
  ],
};