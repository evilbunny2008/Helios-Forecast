"""Helios Forecast without Home Assistant.

Runs the integration's own forecast model (everything under custom_components/helios_forecast that
imports nothing from Home Assistant) on a timer, learns from production history read straight from
the inverter's cloud API instead of the recorder, and serves the result over a small web UI and JSON
API. The model is imported, never copied, so a fix to the integration is a fix here too.
"""
