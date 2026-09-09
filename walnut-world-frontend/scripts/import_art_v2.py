"""Rebuild native theme and SpriteFrames from the checked-in art metadata."""
import json
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
ART = 'res://assets/art/redesign/crop_adaptive/v2/'
OUT = PROJECT / 'resources/ui/art_v2'
OUT.mkdir(parents=True, exist_ok=True)


def style(asset, margin=12, content=10):
    return (f'[gd_resource type="StyleBoxTexture" load_steps=2 format=3]\n\n'
            f'[ext_resource type="Texture2D" path="{ART}components/{asset}.png" id="1"]\n\n'
            '[resource]\ntexture = ExtResource("1")\n' + ''.join(
                f'texture_margin_{side} = {margin}.0\ncontent_margin_{side} = {content}.0\n'
                for side in ('left', 'top', 'right', 'bottom')) +
            'axis_stretch_horizontal = 0\naxis_stretch_vertical = 0\n')


for asset, margin, content in [('ui-panel', 28, 16), ('ui-dialogue', 28, 12),
                              ('ui-code', 28, 14), ('ui-hud', 28, 12),
                              ('ui-data-plaque', 14, 6), ('ui-nameplate', 14, 8)]:
    (OUT / f'{asset}.tres').write_text(style(asset, margin, content), encoding='utf-8')

ext, settings = [], []
for state in ['hover', 'filled', 'error']:
    (OUT / f'input_{state}.tres').write_text(style(f'input-{state}', 10, 8), encoding='utf-8')
for control, prefix, states in [('Button', 'button', ['normal', 'hover', 'pressed', 'disabled', 'focus']),
                                ('LineEdit', 'input', ['normal', 'focus', 'disabled'])]:
    for state in states:
        resource_id = f'{prefix}_{state}'
        visual = style(f'{prefix}-{state}', 14 if prefix == 'button' else 10, 8)
        if prefix == 'input' and state == 'focus':
            # LineEdit draws focus over its contents: keep the center transparent.
            visual = ('[gd_resource type="StyleBoxFlat" format=3]\n\n[resource]\n'
                      'draw_center = false\nborder_color = Color(0.12, 0.40, 0.25, 1)\n' +
                      ''.join(f'border_width_{side} = 3\n' for side in ('left', 'top', 'right', 'bottom')) +
                      ''.join(f'corner_radius_{corner} = 8\n' for corner in ('top_left', 'top_right', 'bottom_left', 'bottom_right')))
        (OUT / f'{resource_id}.tres').write_text(visual, encoding='utf-8')
        ext.append(f'[ext_resource type="StyleBox" path="res://resources/ui/art_v2/{resource_id}.tres" id="{resource_id}"]')
        settings.append(f'{control}/styles/{"read_only" if control == "LineEdit" and state == "disabled" else state} = ExtResource("{resource_id}")')
for weight, filename in [('body', 'NotoSansSC-Medium'), ('bold', 'NotoSansSC-Bold'), ('mono', 'NotoSansMonoCJKsc-Regular')]:
    ext.append(f'[ext_resource type="FontFile" path="res://assets/fonts/noto-sans-sc/{filename}.otf" id="{weight}"]')
for control in ('Button', 'CheckButton', 'Label', 'LineEdit', 'RichTextLabel'):
    settings.append(f'{control}/colors/{"default_color" if control == "RichTextLabel" else "font_color"} = Color(0.075, 0.145, 0.11, 1)')
for color in ['font_hover_color', 'font_pressed_color', 'font_focus_color']:
    settings.append(f'Button/colors/{color} = Color(0.08, 0.22, 0.16, 1)')
settings += [
    'Button/colors/font_disabled_color = Color(0.34, 0.31, 0.26, 1)',
    'Button/fonts/font = ExtResource("bold")', 'Button/font_sizes/font_size = 20',
    'CheckButton/fonts/font = ExtResource("bold")', 'CheckButton/font_sizes/font_size = 18',
    'LineEdit/colors/font_placeholder_color = Color(0.29, 0.32, 0.27, 1)',
    'LineEdit/colors/font_uneditable_color = Color(0.29, 0.32, 0.27, 1)',
    'LineEdit/colors/font_selected_color = Color(1, 1, 0.96, 1)',
    'LineEdit/colors/selection_color = Color(0.12, 0.32, 0.22, 1)',
    'LineEdit/colors/caret_color = Color(0.075, 0.145, 0.11, 1)',
    'LineEdit/constants/caret_width = 2', 'LineEdit/font_sizes/font_size = 20',
    'CodeEdit/fonts/font = ExtResource("mono")', 'CodeEdit/font_sizes/font_size = 20',
    'CodeEdit/colors/line_number_color = Color(0.73, 0.82, 0.76, 1)',
    'CodeEdit/constants/line_spacing = 4',
    'Label/constants/line_spacing = 3',
    'RichTextLabel/fonts/normal_font = ExtResource("body")',
    'RichTextLabel/fonts/bold_font = ExtResource("bold")',
    'RichTextLabel/fonts/mono_font = ExtResource("mono")',
    'RichTextLabel/font_sizes/normal_font_size = 20',
    'RichTextLabel/font_sizes/bold_font_size = 20',
    'RichTextLabel/constants/line_separation = 4',
    'TitleLabel/base_type = &"Label"', 'TitleLabel/fonts/font = ExtResource("bold")',
]
(OUT / 'theme.tres').write_text(f'[gd_resource type="Theme" load_steps={len(ext)+1} format=3]\n\n' + '\n'.join(ext) + '\n\n[resource]\ndefault_font = ExtResource("body")\ndefault_font_size = 20\n' + '\n'.join(settings) + '\n', encoding='utf-8')

# Preserve the existing AnimatedSprite2D and its completion await. Only its visual
# resource changes; WATER decisions still belong to the existing controller.
ext, sub, anim = [], [], []
for units in (1, 2):
    clip = f'tool-adaptive-water-{"one" if units == 1 else "two"}'
    meta = json.loads((PROJECT / ART.removeprefix('res://') / 'motion/metadata' / f'{clip}.json').read_text(encoding='utf-8'))
    ext.append(f'[ext_resource type="Texture2D" path="{ART}motion/{meta["files"]["atlas"]}" id="atlas{units}"]')
    frames = []
    for index, frame in enumerate(meta['atlas']['frames']):
        sid = f'f{units}_{index}'
        sub.append(f'[sub_resource type="AtlasTexture" id="{sid}"]\natlas = ExtResource("atlas{units}")\nregion = Rect2({", ".join(map(str, frame["rect"]))})\nfilter_clip = true')
        frames.append('{"duration": ' + str(frame['durationMs'] / 1000) + ', "texture": SubResource("' + sid + '")}')
    anim.append('{"frames": [' + ', '.join(frames) + '], "loop": false, "name": &"' + ('pour' if units == 1 else 'pour_two') + '", "speed": 1.0}')
(OUT / 'watering_frames.tres').write_text(f'[gd_resource type="SpriteFrames" load_steps={len(ext)+len(sub)+1} format=3]\n\n' + '\n'.join(ext) + '\n\n' + '\n\n'.join(sub) + '\n\n[resource]\nanimations = [' + ',\n'.join(anim) + ']\n', encoding='utf-8')
print('ART_V2_RESOURCES_BUILT')
