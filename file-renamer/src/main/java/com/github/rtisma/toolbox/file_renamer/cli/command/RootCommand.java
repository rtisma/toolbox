package com.github.rtisma.toolbox.file_renamer.cli.command;

import java.util.concurrent.Callable;

import com.github.rtisma.toolbox.file_renamer.cli.util.VersionProvider;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import picocli.CommandLine;
import picocli.CommandLine.Command;

@Slf4j
@RequiredArgsConstructor
@Command(
    name = "file-renamer",
    mixinStandardHelpOptions = true,
    versionProvider = VersionProvider.class,
    description = "Main command")
public class RootCommand implements Callable<Integer>
{

  @Override
  public Integer call() throws Exception {
    CommandLine.usage(this, System.out);
    return 0;
  }
}
