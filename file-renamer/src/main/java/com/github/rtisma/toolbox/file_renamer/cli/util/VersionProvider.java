package com.github.rtisma.toolbox.file_renamer.cli.util;

import picocli.CommandLine.IVersionProvider;

public class VersionProvider implements IVersionProvider {

  private static final String TEMP_VERSION = "0.0.1";

  @Override
  public String[] getVersion() throws Exception {
    return new String[] {TEMP_VERSION};
  }
}
