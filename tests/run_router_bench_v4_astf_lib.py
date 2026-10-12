#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Compatibility exports for the shared ASTF implementation."""

try:
    from tests.lib.astf import *
except Exception:
    from lib.astf import *
