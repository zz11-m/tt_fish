# SPDX-License-Identifier: GPL-3.0-only
"""由局部控件构造无玩家数据的视觉测试场景。"""
import cv2
import numpy as np
from fisher.config import ROOT
from fisher.vision import TEMPLATE_DEFINITIONS, read_image


def make_frame(state, variant="blue"):
    image = np.full((598,336,3), (30,30,30), dtype=np.uint8)
    def paste(name):
        (x1,y1,x2,y2),_ = TEMPLATE_DEFINITIONS[name]
        image[y1:y2,x1:x2] = read_image(ROOT/f"assets/templates/{name}.png")
    if state == "IDLE":
        paste("idle")
    elif state == "WAIT":
        image[455:524,131:203] = (100,100,100)
        paste("wait")
    elif state == "BITE":
        image[356:387,88:253] = (220,40,170)
        paste("bite")
        paste("purple")
    elif state == "TIMING":
        paste("purple")
        paste("cap")
        image[397:408,172:245] = (0,235,255)
        image[384:414,99:107] = (20,40,220)
    elif state == "RESULT":
        colors = {"blue": (240,150,30), "purple": (230,20,180), "gold": (35,200,255),
                  "red": (60,40,240), "red_wrap": (80,40,240)}
        image[180:400,10:326] = colors[variant]
        paste("weight")
        image[475:542,125:215] = (20,20,20)
    elif state == "CLAIM":
        image[:] = 15
        image[507:548,96:242] = (30,210,135)
        paste("claim_title")
        paste("claim_button")
    return image
