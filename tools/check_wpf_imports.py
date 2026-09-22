# -*- coding: utf-8 -*-
"""Catch WPF types imported from the wrong namespace.

Why this exists
---------------
DeeMAPLink shipped with `from System.Windows import Cursors`. Cursors
looks like it belongs beside Thickness and CornerRadius, but it lives in
System.Windows.Input next to Keyboard. The tool failed to open at all:

    ImportError: Cannot import name Cursors

Nothing catches this before Revit does. ast.parse is happy - the syntax
is fine. An "is every name imported?" check is happy - the name IS
imported. Only the .NET loader knows, and by then a user has clicked a
button that does nothing but throw a traceback.

A NOTE ON THE FIRST VERSION OF THIS FILE
----------------------------------------
It mapped each type name to ONE namespace and flagged anything else. That
immediately reported 21 errors that were not errors: every
`from System.Windows.Forms import MessageBox` in the extension. WinForms
has its own MessageBox, quite legitimately, and several type names exist
in both frameworks.

So the model here is "is this name valid in the namespace it was imported
FROM", not "where does this name belong". A name can be valid in several
places. Only WPF namespaces this file actually knows are checked;
System.Windows.Forms and the other System.Windows.* families are listed
as deliberately-not-checked rather than guessed at, because a checker
that cries wolf gets ignored, and an ignored checker is worse than none.

    python tools/check_wpf_imports.py

Exits non-zero only for a name that is genuinely in the wrong WPF
namespace.
"""
import ast
import io
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# namespace -> the type names it declares (the ones this codebase uses)
VALID = {}


def _add(namespace, names):
    VALID.setdefault(namespace, set()).update(names.split())


_add("System.Windows", """
    Application Clipboard CornerRadius DataObject DataFormats DependencyObject
    DependencyProperty DragDrop DragDropEffects Duration FontStyles
    FontWeights FrameworkElement GridLength GridUnitType HorizontalAlignment
    Int32Rect MessageBox MessageBoxButton MessageBoxImage MessageBoxResult
    Point PropertyPath Rect ResizeMode ResourceDictionary RoutedEventArgs
    Setter Size SizeToContent Style SystemParameters TextAlignment
    TextDecorations TextTrimming TextWrapping Thickness UIElement Vector
    VerticalAlignment Visibility Window WindowStartupLocation WindowState
    WindowStyle
""")
_add("System.Windows.Input", """
    ApplicationCommands CommandBinding Cursor Cursors ICommand InputManager
    Key KeyEventArgs Keyboard ModifierKeys Mouse MouseButton
    MouseButtonEventArgs MouseButtonState MouseEventArgs MouseWheelEventArgs
""")
_add("System.Windows.Media", """
    Brush Brushes Color ColorConverter DoubleCollection FontFamily Geometry
    ImageSource Matrix PathGeometry PointCollection RectangleGeometry
    RotateTransform ScaleTransform SolidColorBrush Stretch TransformGroup
    TranslateTransform VisualTreeHelper
""")
_add("System.Windows.Controls", """
    Border Button Canvas CheckBox ColumnDefinition ComboBox ComboBoxItem
    ContentControl DataGrid DataGridLength DataGridLengthUnitType
    DataGridSelectionMode DataGridSelectionUnit DataGridTextColumn Dock
    DockPanel Grid Image Label ListBox ListBoxItem ListView Orientation Panel
    ProgressBar RadioButton ScrollViewer SelectionChangedEventArgs Slider
    StackPanel TabControl TabItem TextBlock TextBox ToolTip TreeView
    UserControl WrapPanel
""")
_add("System.Windows.Shapes", "Ellipse Line Path Polygon Polyline Rectangle")
_add("System.Windows.Threading",
     "Dispatcher DispatcherFrame DispatcherPriority DispatcherTimer")
_add("System.Windows.Media.Imaging", "BitmapCacheOption BitmapImage BitmapSource BitmapSizeOptions")
_add("System.Windows.Media.Effects", "DropShadowEffect")
_add("System.Windows.Data", "Binding BindingMode IValueConverter UpdateSourceTrigger")
_add("System.Windows.Documents", "Hyperlink Run")
_add("System.Windows.Interop", "WindowInteropHelper Imaging")
_add("System.Windows.Shell", "WindowChrome")

# Not WPF, or not modelled here. Imports from these are left alone rather
# than guessed at - see the note above about crying wolf.
NOT_CHECKED = ("System.Windows.Forms", "System.Windows.Automation")


def python_files(root):
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in (".git", "__pycache__")]
        for name in files:
            if name.endswith(".py"):
                yield os.path.join(dirpath, name)


def main():
    wrong = []
    unknown = []
    checked = skipped = 0

    for path in python_files(ROOT):
        rel = os.path.relpath(path, ROOT)
        try:
            tree = ast.parse(io.open(path, encoding="utf-8").read())
        except Exception:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or not node.module:
                continue
            module = node.module
            if not module.startswith("System.Windows"):
                continue
            if any(module.startswith(ns) for ns in NOT_CHECKED):
                skipped += len(node.names)
                continue
            known = VALID.get(module)
            if known is None:
                skipped += len(node.names)
                continue
            for alias in node.names:
                checked += 1
                if alias.name in known:
                    continue
                elsewhere = [ns for ns, names in VALID.items()
                             if alias.name in names]
                if elsewhere:
                    wrong.append((rel, node.lineno, alias.name, module,
                                  elsewhere[0]))
                else:
                    unknown.append((rel, node.lineno, alias.name, module))

    print("WPF imports checked : {0}   (not modelled, skipped: {1})".format(
        checked, skipped))
    if wrong:
        print("WRONG NAMESPACE ({0}) - these fail at load:".format(len(wrong)))
        for rel, line, name, got, want in wrong:
            print("  {0}:{1}\n      {2} imported from {3}, declared in {4}"
                  .format(rel, line, name, got, want))
    else:
        print("WRONG NAMESPACE     : none")

    if unknown:
        print("not in the table ({0}) - check by hand, then add them here:"
              .format(len(unknown)))
        for rel, line, name, got in unknown:
            print("  {0}:{1}  {2}  (from {3})".format(rel, line, name, got))

    return 1 if wrong else 0


if __name__ == "__main__":
    sys.exit(main())
